# Copyright 2020 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib
import logging
import string
from abc import abstractmethod
from functools import lru_cache
from typing import List, Optional
from unittest import mock, skipIf

from transformers import (
    FEATURE_EXTRACTOR_MAPPING,
    TOKENIZER_MAPPING,
    AutoFeatureExtractor,
    AutoTokenizer,
    is_tf_available,
    is_torch_available,
    pipeline,
)
from transformers.file_utils import to_py_obj
from transformers.pipelines import Pipeline
from transformers.testing_utils import _run_slow_tests, is_pipeline_test, require_tf, require_torch, slow


logger = logging.getLogger(__name__)


def get_checkpoint_from_architecture(architecture):
    try:
        module = importlib.import_module(architecture.__module__)
    except ImportError:
        logger.error(f"Ignoring architecture {architecture}")
        return

    if hasattr(module, "_CHECKPOINT_FOR_DOC"):
        return module._CHECKPOINT_FOR_DOC
    else:
        logger.warning(f"Can't retrieve checkpoint from {architecture.__name__}")


def get_tiny_config_from_class(configuration_class):
    if "OpenAIGPT" in configuration_class.__name__:
        # This is the only file that is inconsistent with the naming scheme.
        # Will rename this file if we decide this is the way to go
        return

    model_type = configuration_class.model_type
    camel_case_model_name = configuration_class.__name__.split("Config")[0]

    try:
        module = importlib.import_module(f".test_modeling_{model_type.replace('-', '_')}", package="tests")
        model_tester_class = getattr(module, f"{camel_case_model_name}ModelTester", None)
    except (ImportError, AttributeError):
        logger.error(f"No model tester class for {configuration_class.__name__}")
        return

    if model_tester_class is None:
        logger.warning(f"No model tester class for {configuration_class.__name__}")
        return

    model_tester = model_tester_class(parent=None)

    if hasattr(model_tester, "get_pipeline_config"):
        return model_tester.get_pipeline_config()
    elif hasattr(model_tester, "get_config"):
        return model_tester.get_config()
    else:
        logger.warning(f"Model tester {model_tester_class.__name__} has no `get_config()`.")


@lru_cache(maxsize=100)
def get_tiny_tokenizer_from_checkpoint(checkpoint):
    tokenizer = AutoTokenizer.from_pretrained(checkpoint)
    logger.info("Training new from iterator ...")
    vocabulary = string.ascii_letters + string.digits + " "
    tokenizer = tokenizer.train_new_from_iterator(vocabulary, vocab_size=len(vocabulary), show_progress=False)
    logger.info("Trained.")
    return tokenizer


def get_tiny_feature_extractor_from_checkpoint(checkpoint, tiny_config):
    try:
        feature_extractor = AutoFeatureExtractor.from_pretrained(checkpoint)
    except Exception:
        feature_extractor = None
    if hasattr(tiny_config, "image_size") and feature_extractor:
        feature_extractor = feature_extractor.__class__(size=tiny_config.image_size, crop_size=tiny_config.image_size)
    return feature_extractor


class ANY:
    def __init__(self, _type):
        self._type = _type

    def __eq__(self, other):
        return isinstance(other, self._type)

    def __repr__(self):
        return f"ANY({self._type.__name__})"


class PipelineTestCaseMeta(type):
    def __new__(mcs, name, bases, dct):
        def gen_test(ModelClass, checkpoint, tiny_config, tokenizer_class, feature_extractor_class):
            @skipIf(tiny_config is None, "TinyConfig does not exist")
            @skipIf(checkpoint is None, "checkpoint does not exist")
            def test(self):
                if ModelClass.__name__.endswith("ForCausalLM"):
                    tiny_config.is_encoder_decoder = False
                if ModelClass.__name__.endswith("WithLMHead"):
                    tiny_config.is_decoder = True
                model = ModelClass(tiny_config)
                if hasattr(model, "eval"):
                    model = model.eval()
                if tokenizer_class is not None:
                    try:
                        tokenizer = get_tiny_tokenizer_from_checkpoint(checkpoint)
                        if hasattr(model.config, "max_position_embeddings"):
                            tokenizer.model_max_length = model.config.max_position_embeddings
                    # Rust Panic exception are NOT Exception subclass
                    # Some test tokenizer contain broken vocabs or custom PreTokenizer, so we
                    # provide some default tokenizer and hope for the best.
                    except:  # noqa: E722
                        self.skipTest(f"Ignoring {ModelClass}, cannot create a simple tokenizer")
                else:
                    tokenizer = None
                feature_extractor = get_tiny_feature_extractor_from_checkpoint(checkpoint, tiny_config)
                self.run_pipeline_test(model, tokenizer, feature_extractor)

            return test

        for prefix, key in [("pt", "model_mapping"), ("tf", "tf_model_mapping")]:
            mapping = dct.get(key, {})
            if mapping:
                for configuration, model_architectures in mapping.items():
                    if not isinstance(model_architectures, tuple):
                        model_architectures = (model_architectures,)

                    for model_architecture in model_architectures:
                        checkpoint = get_checkpoint_from_architecture(model_architecture)
                        tiny_config = get_tiny_config_from_class(configuration)
                        tokenizer_classes = TOKENIZER_MAPPING.get(configuration, [])
                        feature_extractor_class = FEATURE_EXTRACTOR_MAPPING.get(configuration, None)
                        feature_extractor_name = (
                            feature_extractor_class.__name__ if feature_extractor_class else "nofeature_extractor"
                        )
                        if not tokenizer_classes:
                            # We need to test even if there are no tokenizers.
                            tokenizer_classes = [None]
                        for tokenizer_class in tokenizer_classes:
                            if tokenizer_class is not None:
                                tokenizer_name = tokenizer_class.__name__
                            else:
                                tokenizer_name = "notokenizer"

                            test_name = f"test_{prefix}_{configuration.__name__}_{model_architecture.__name__}_{tokenizer_name}_{feature_extractor_name}"

                            if tokenizer_class is not None or feature_extractor_class is not None:
                                dct[test_name] = gen_test(
                                    model_architecture,
                                    checkpoint,
                                    tiny_config,
                                    tokenizer_class,
                                    feature_extractor_class,
                                )

        @abstractmethod
        def inner(self):
            raise NotImplementedError("Not implemented test")

        # Force these 2 methods to exist
        dct["test_small_model_pt"] = dct.get("test_small_model_pt", inner)
        dct["test_small_model_tf"] = dct.get("test_small_model_tf", inner)

        return type.__new__(mcs, name, bases, dct)


VALID_INPUTS = ["A simple string", ["list of strings"]]


@is_pipeline_test
class CustomInputPipelineCommonMixin:
    pipeline_task = None
    pipeline_loading_kwargs = {}  # Additional kwargs to load the pipeline with
    pipeline_running_kwargs = {}  # Additional kwargs to run the pipeline with
    small_models = []  # Models tested without the @slow decorator
    large_models = []  # Models tested with the @slow decorator
    valid_inputs = VALID_INPUTS  # Some inputs which are valid to compare fast and slow tokenizers

    def setUp(self) -> None:
        if not is_tf_available() and not is_torch_available():
            return  # Currently no JAX pipelines

        # Download needed checkpoints
        models = self.small_models
        if _run_slow_tests:
            models = models + self.large_models

        for model_name in models:
            if is_torch_available():
                pipeline(
                    self.pipeline_task,
                    model=model_name,
                    tokenizer=model_name,
                    framework="pt",
                    **self.pipeline_loading_kwargs,
                )
            if is_tf_available():
                pipeline(
                    self.pipeline_task,
                    model=model_name,
                    tokenizer=model_name,
                    framework="tf",
                    **self.pipeline_loading_kwargs,
                )

    @require_torch
    @slow
    def test_pt_defaults(self):
        pipeline(self.pipeline_task, framework="pt", **self.pipeline_loading_kwargs)

    @require_tf
    @slow
    def test_tf_defaults(self):
        pipeline(self.pipeline_task, framework="tf", **self.pipeline_loading_kwargs)

    @require_torch
    def test_torch_small(self):
        for model_name in self.small_models:
            pipe_small = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="pt",
                **self.pipeline_loading_kwargs,
            )
            self._test_pipeline(pipe_small)

    @require_tf
    def test_tf_small(self):
        for model_name in self.small_models:
            pipe_small = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="tf",
                **self.pipeline_loading_kwargs,
            )
            self._test_pipeline(pipe_small)

    @require_torch
    @slow
    def test_torch_large(self):
        for model_name in self.large_models:
            pipe_large = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="pt",
                **self.pipeline_loading_kwargs,
            )
            self._test_pipeline(pipe_large)

    @require_tf
    @slow
    def test_tf_large(self):
        for model_name in self.large_models:
            pipe_large = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="tf",
                **self.pipeline_loading_kwargs,
            )
            self._test_pipeline(pipe_large)

    def _test_pipeline(self, pipe: Pipeline):
        raise NotImplementedError

    @require_torch
    def test_compare_slow_fast_torch(self):
        for model_name in self.small_models:
            pipe_slow = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="pt",
                use_fast=False,
                **self.pipeline_loading_kwargs,
            )
            pipe_fast = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="pt",
                use_fast=True,
                **self.pipeline_loading_kwargs,
            )
            self._compare_slow_fast_pipelines(pipe_slow, pipe_fast, method="forward")

    @require_tf
    def test_compare_slow_fast_tf(self):
        for model_name in self.small_models:
            pipe_slow = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="tf",
                use_fast=False,
                **self.pipeline_loading_kwargs,
            )
            pipe_fast = pipeline(
                task=self.pipeline_task,
                model=model_name,
                tokenizer=model_name,
                framework="tf",
                use_fast=True,
                **self.pipeline_loading_kwargs,
            )
            self._compare_slow_fast_pipelines(pipe_slow, pipe_fast, method="call")

    def _compare_slow_fast_pipelines(self, pipe_slow: Pipeline, pipe_fast: Pipeline, method: str):
        """We check that the inputs to the models forward passes are identical for
        slow and fast tokenizers.
        """
        with mock.patch.object(
            pipe_slow.model, method, wraps=getattr(pipe_slow.model, method)
        ) as mock_slow, mock.patch.object(
            pipe_fast.model, method, wraps=getattr(pipe_fast.model, method)
        ) as mock_fast:
            for inputs in self.valid_inputs:
                if isinstance(inputs, dict):
                    inputs.update(self.pipeline_running_kwargs)
                    _ = pipe_slow(**inputs)
                    _ = pipe_fast(**inputs)
                else:
                    _ = pipe_slow(inputs, **self.pipeline_running_kwargs)
                    _ = pipe_fast(inputs, **self.pipeline_running_kwargs)

                mock_slow.assert_called()
                mock_fast.assert_called()

                self.assertEqual(len(mock_slow.call_args_list), len(mock_fast.call_args_list))
                for mock_slow_call_args, mock_fast_call_args in zip(
                    mock_slow.call_args_list, mock_slow.call_args_list
                ):
                    slow_call_args, slow_call_kwargs = mock_slow_call_args
                    fast_call_args, fast_call_kwargs = mock_fast_call_args

                    slow_call_args, slow_call_kwargs = to_py_obj(slow_call_args), to_py_obj(slow_call_kwargs)
                    fast_call_args, fast_call_kwargs = to_py_obj(fast_call_args), to_py_obj(fast_call_kwargs)

                    self.assertEqual(slow_call_args, fast_call_args)
                    self.assertDictEqual(slow_call_kwargs, fast_call_kwargs)


@is_pipeline_test
class MonoInputPipelineCommonMixin(CustomInputPipelineCommonMixin):
    """A version of the CustomInputPipelineCommonMixin
    with a predefined `_test_pipeline` method.
    """

    mandatory_keys = {}  # Keys which should be in the output
    invalid_inputs = [None]  # inputs which are not allowed
    expected_multi_result: Optional[List] = None
    expected_check_keys: Optional[List[str]] = None

    def _test_pipeline(self, pipe: Pipeline):
        self.assertIsNotNone(pipe)

        mono_result = pipe(self.valid_inputs[0], **self.pipeline_running_kwargs)
        self.assertIsInstance(mono_result, list)
        self.assertIsInstance(mono_result[0], (dict, list))

        if isinstance(mono_result[0], list):
            mono_result = mono_result[0]

        for key in self.mandatory_keys:
            self.assertIn(key, mono_result[0])

        multi_result = [pipe(input, **self.pipeline_running_kwargs) for input in self.valid_inputs]
        self.assertIsInstance(multi_result, list)
        self.assertIsInstance(multi_result[0], (dict, list))

        if self.expected_multi_result is not None:
            for result, expect in zip(multi_result, self.expected_multi_result):
                for key in self.expected_check_keys or []:
                    self.assertEqual(
                        set([o[key] for o in result]),
                        set([o[key] for o in expect]),
                    )

        if isinstance(multi_result[0], list):
            multi_result = multi_result[0]

        for result in multi_result:
            for key in self.mandatory_keys:
                self.assertIn(key, result)

        self.assertRaises(Exception, pipe, self.invalid_inputs)

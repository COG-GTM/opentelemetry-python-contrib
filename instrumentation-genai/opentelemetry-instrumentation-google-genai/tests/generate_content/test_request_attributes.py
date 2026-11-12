# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import unittest
from unittest import mock

from google.genai.types import (
    FunctionDeclaration,
    GenerateContentConfig,
    HarmBlockThreshold,
    HarmCategory,
    SafetySetting,
    Schema,
    Tool,
    Type,
)

from opentelemetry.instrumentation.google_genai.allowlist_util import AllowList
from opentelemetry.instrumentation.google_genai.generate_content import (
    _create_request_attributes,
)

_CONFIG_KWARGS = {
    "temperature": 0.25,
    "top_k": 20,
    "top_p": 0.5,
    "candidate_count": 2,
    "max_output_tokens": 512,
    "stop_sequences": ["foo", "bar"],
    "frequency_penalty": 0.1,
    "presence_penalty": 0.2,
    "seed": 12345,
    "response_mime_type": "application/json",
    "system_instruction": "Be terse.",
    "safety_settings": [
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH,
        )
    ],
    "tools": [
        Tool(
            function_declarations=[
                FunctionDeclaration(
                    name="get_weather",
                    description="Returns the weather.",
                    parameters=Schema(
                        type=Type.OBJECT,
                        properties={"city": Schema(type=Type.STRING)},
                        required=["city"],
                    ),
                )
            ]
        )
    ],
}

_EXPECTED_DEFAULT_ATTRIBUTES = {
    "gen_ai.request.temperature": 0.25,
    "gen_ai.request.top_k": 20,
    "gen_ai.request.top_p": 0.5,
    "gen_ai.request.choice.count": 2,
    "gen_ai.request.max_tokens": 512,
    "gen_ai.request.stop_sequences": ["foo", "bar"],
    "gen_ai.request.frequency_penalty": 0.1,
    "gen_ai.request.presence_penalty": 0.2,
    "gen_ai.request.seed": 12345,
    "gen_ai.output.type": "json",
}

# Has includes (so the full config is flattened) but matches no config key,
# so it must yield exactly the same attributes as the default allowlist.
_NON_MATCHING_ALLOW_LIST = AllowList(includes=["does.not.match"])


class RequestAttributesTestCase(unittest.TestCase):
    def _configs(self):
        return {
            "object": GenerateContentConfig(**_CONFIG_KWARGS),
            "dict": dict(_CONFIG_KWARGS),
        }

    def test_default_allowlist_records_only_semconv_attributes(self):
        for name, config in self._configs().items():
            with self.subTest(config=name):
                attributes = _create_request_attributes(config, AllowList())
                self.assertEqual(attributes, _EXPECTED_DEFAULT_ATTRIBUTES)

    def test_default_allowlist_matches_full_flatten(self):
        for name, config in self._configs().items():
            with self.subTest(config=name):
                pruned = _create_request_attributes(config, AllowList())
                full = _create_request_attributes(
                    config, _NON_MATCHING_ALLOW_LIST
                )
                self.assertEqual(
                    {
                        key: (type(value), value)
                        for key, value in pruned.items()
                    },
                    {key: (type(value), value) for key, value in full.items()},
                )

    def test_default_allowlist_skips_full_config_dump(self):
        config = GenerateContentConfig(**_CONFIG_KWARGS)
        with mock.patch.object(
            GenerateContentConfig,
            "model_dump",
            autospec=True,
            side_effect=GenerateContentConfig.model_dump,
        ) as model_dump:
            _create_request_attributes(config, AllowList())
        model_dump.assert_called_once()
        include = model_dump.call_args.kwargs["include"]
        self.assertNotIn("tools", include)
        self.assertNotIn("safety_settings", include)
        self.assertNotIn("system_instruction", include)

    def test_text_plain_and_custom_mime_types(self):
        for mime_type, expected in (
            ("text/plain", "text"),
            ("text/x.enum", "text/x.enum"),
        ):
            with self.subTest(mime_type=mime_type):
                attributes = _create_request_attributes(
                    {"response_mime_type": mime_type}, AllowList()
                )
                self.assertEqual(attributes, {"gen_ai.output.type": expected})

    def test_config_without_recorded_fields_yields_no_attributes(self):
        config = {"tools": _CONFIG_KWARGS["tools"], "labels": {"a": "b"}}
        self.assertEqual(_create_request_attributes(config, AllowList()), {})

    def test_wildcard_allowlist_still_records_custom_config_keys(self):
        config = GenerateContentConfig(**_CONFIG_KWARGS)
        attributes = _create_request_attributes(
            config, AllowList(includes=["*"])
        )
        for key, value in _EXPECTED_DEFAULT_ATTRIBUTES.items():
            self.assertEqual(attributes[key], value)
        self.assertEqual(
            attributes[
                "gcp.gen_ai.operation.config.tools[0].function_declarations[0].name"
            ],
            "get_weather",
        )
        self.assertEqual(
            attributes["gcp.gen_ai.operation.config.response_mime_type"],
            "application/json",
        )
        self.assertNotIn(
            "gcp.gen_ai.operation.config.system_instruction", attributes
        )

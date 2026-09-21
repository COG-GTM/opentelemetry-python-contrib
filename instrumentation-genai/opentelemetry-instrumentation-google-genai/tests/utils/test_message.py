# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import unittest

from google.genai import types as genai_types

from opentelemetry.instrumentation.google_genai.message import (
    to_input_messages,
)


class ToPartTest(unittest.TestCase):
    def test_unhandled_part_is_dropped_without_logging_content(self):
        code = "print('secret user content')"
        content = genai_types.Content(
            role="model",
            parts=[
                genai_types.Part(
                    executable_code=genai_types.ExecutableCode(
                        code=code,
                        language=genai_types.Language.PYTHON,
                    )
                )
            ],
        )

        with self.assertLogs(
            "opentelemetry.instrumentation.google_genai.message",
            level=logging.DEBUG,
        ) as logs:
            messages = to_input_messages(contents=[content])

        self.assertEqual(messages[0].parts, [])
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.levelno, logging.DEBUG)
        self.assertNotIn(code, record.getMessage())
        self.assertIn("executable_code", record.getMessage())

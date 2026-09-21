# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import unittest

from opentelemetry.util.http import redact_query_string


class TestRedactQueryString(unittest.TestCase):
    def test_redact_signature(self):
        self.assertEqual(
            redact_query_string("color=blue&sig=secret"),
            "color=blue&sig=REDACTED",
        )

    def test_redact_goog_signature(self):
        self.assertEqual(
            redact_query_string("X-Goog-Signature=secret"),
            "X-Goog-Signature=REDACTED",
        )

    def test_no_redaction_needed_returns_original(self):
        query_string = "color=blue&query=secret&empty="
        self.assertEqual(redact_query_string(query_string), query_string)

    def test_empty_query_string(self):
        self.assertEqual(redact_query_string(""), "")

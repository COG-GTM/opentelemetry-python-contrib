# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import unittest
from unittest.mock import patch

import opentelemetry.util.http as http_util
from opentelemetry.util.http import (
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS,
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST,
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE,
    SanitizeValue,
    get_custom_header_attributes,
    get_custom_headers,
    normalise_request_header_name,
    normalise_response_header_name,
)


class TestCaptureCustomHeaders(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST: "User-Agent,Test-Header"
        },
    )
    def test_get_custom_request_header(self):
        custom_headers_to_capture = get_custom_headers(
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST
        )
        self.assertEqual(
            custom_headers_to_capture, ["User-Agent", "Test-Header"]
        )

    @patch.dict(
        "os.environ",
        {
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE: "content-type,content-length,test-header"
        },
    )
    def test_get_custom_response_header(self):
        custom_headers_to_capture = get_custom_headers(
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE
        )
        self.assertEqual(
            custom_headers_to_capture,
            [
                "content-type",
                "content-length",
                "test-header",
            ],
        )

    @patch.dict(
        "os.environ",
        {
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS: "My-Secret-Header,My-Secret-Header-2"
        },
    )
    def test_get_custom_sanitize_header(self):
        sanitized_fields = get_custom_headers(
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS
        )
        self.assertEqual(
            sanitized_fields,
            ["My-Secret-Header", "My-Secret-Header-2"],
        )

    @patch.dict(
        "os.environ",
        {
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS: "My-Secret-Header,My-Secret-Header-2"
        },
    )
    def test_sanitize(self):
        sanitized_fields = get_custom_headers(
            OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS
        )

        sanitize = SanitizeValue(sanitized_fields)

        self.assertEqual(
            sanitize.sanitize_header_value(
                header="My-Secret-Header", value="My-Secret-Value"
            ),
            "[REDACTED]",
        )

        self.assertEqual(
            sanitize.sanitize_header_value(
                header="My-Not-Secret-Header", value="My-Not-Secret-Value"
            ),
            "My-Not-Secret-Value",
        )

    def test_normalise_request_header_name(self):
        key = normalise_request_header_name("Test-Header")
        self.assertEqual(key, "http.request.header.test_header")

    def test_normalise_response_header_name(self):
        key = normalise_response_header_name("Test-Header")
        self.assertEqual(key, "http.response.header.test_header")

    def test_get_custom_header_attributes(self):
        headers = {
            "User-Agent": "test-agent",
            "My-Secret-Header": "secret",
            "X-Multi": ["a", "b"],
            "Not-Captured": "ignored",
        }
        attributes = get_custom_header_attributes(
            headers,
            ["user-agent", "my-secret-.*", "x-multi"],
            ["MY-SECRET-HEADER"],
            normalise_request_header_name,
        )
        self.assertEqual(
            attributes,
            {
                "http.request.header.user_agent": ["test-agent"],
                "http.request.header.my_secret_header": ["[REDACTED]"],
                "http.request.header.x_multi": ["a", "b"],
            },
        )

    def test_get_custom_header_attributes_no_sensitive_headers(self):
        attributes = get_custom_header_attributes(
            {"My-Secret-Header": "secret"},
            ["my-secret-header"],
            None,
            normalise_response_header_name,
        )
        self.assertEqual(
            attributes,
            {"http.response.header.my_secret_header": ["secret"]},
        )

    def test_get_custom_header_attributes_reuses_compiled_regexes(self):
        http_util._compile_header_regexes.cache_clear()
        http_util._get_sanitize_value.cache_clear()
        headers = {"User-Agent": "test-agent", "Authorization": "token"}
        captured_headers = ["user-agent", "authorization"]
        sensitive_headers = ["authorization"]

        with patch.object(
            http_util, "re_compile", wraps=http_util.re_compile
        ) as re_compile:
            results = [
                get_custom_header_attributes(
                    headers,
                    list(captured_headers),
                    list(sensitive_headers),
                    normalise_request_header_name,
                )
                for _ in range(3)
            ]

        self.assertEqual(re_compile.call_count, 2)
        for attributes in results:
            self.assertEqual(
                attributes,
                {
                    "http.request.header.user_agent": ["test-agent"],
                    "http.request.header.authorization": ["[REDACTED]"],
                },
            )

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from unittest import TestCase

from opentelemetry.instrumentation.asgi import (
    ASGIGetter,
    _RequestHeaders,
    _RequestHeadersGetter,
)


class TestASGIGetter(TestCase):
    def test_get_none_empty_carrier(self):
        getter = ASGIGetter()
        carrier = {}
        val = getter.get(carrier, "test")
        self.assertIsNone(val)

    def test_get_none_empty_headers(self):
        getter = ASGIGetter()
        carrier = {"headers": []}
        val = getter.get(carrier, "test")
        self.assertIsNone(val)

    def test_get_(self):
        getter = ASGIGetter()
        carrier = {"headers": [(b"test-key", b"val")]}
        expected_val = ["val"]
        self.assertEqual(
            getter.get(carrier, "Test-Key"),
            expected_val,
            "Should be case insensitive",
        )
        self.assertEqual(
            getter.get(carrier, "test-key"),
            expected_val,
            "Should be case insensitive",
        )
        self.assertEqual(
            getter.get(carrier, "TEST-KEY"),
            expected_val,
            "Should be case insensitive",
        )

    def test_keys_empty_carrier(self):
        getter = ASGIGetter()
        keys = getter.keys({})
        self.assertEqual(keys, [])

    def test_keys_empty_headers(self):
        getter = ASGIGetter()
        keys = getter.keys({"headers": []})
        self.assertEqual(keys, [])

    def test_keys(self):
        getter = ASGIGetter()
        carrier = {"headers": [(b"test-key", b"val")]}
        expected_val = ["test-key"]
        self.assertEqual(
            getter.keys(carrier),
            expected_val,
            "Should be equal",
        )

    def test_non_utf8_headers(self):
        getter = ASGIGetter()
        carrier = {"headers": [(b"test-key", b"Moto Z\xb2")]}
        expected_val = ["Moto Z²"]
        self.assertEqual(
            getter.get(carrier, "test-key"),
            expected_val,
            "Should be equal",
        )


class TestRequestHeadersGetter(TestCase):
    def test_matches_asgi_getter(self):
        carrier = {
            "headers": [
                (b"Test-Key", b"val1"),
                (b"other", b"x"),
                (b"test-key", b"val2"),
                (b"non-utf8", b"\xe9"),
            ]
        }
        asgi_getter = ASGIGetter()
        getter = _RequestHeadersGetter()
        request_headers = _RequestHeaders(carrier)
        for key in ("test-key", "TEST-KEY", "other", "non-utf8", "missing"):
            self.assertEqual(
                getter.get(request_headers, key),
                asgi_getter.get(carrier, key),
                key,
            )
        self.assertEqual(
            getter.get(request_headers, "test-key"), ["val1", "val2"]
        )
        self.assertEqual(
            getter.keys(request_headers), asgi_getter.keys(carrier)
        )

    def test_empty(self):
        getter = _RequestHeadersGetter()
        for carrier in ({}, {"headers": []}, {"headers": None}):
            request_headers = _RequestHeaders(carrier)
            self.assertIsNone(getter.get(request_headers, "test"))
            self.assertEqual(getter.keys(request_headers), [])

    def test_unrequested_values_not_decoded(self):
        request_headers = _RequestHeaders(
            {"headers": [(b"cookie", b"\xe9\\u"), (b"host", b"example.com")]}
        )
        self.assertEqual(request_headers.get("host"), ["example.com"])
        self.assertEqual(request_headers.names, ["cookie", "host"])

    def test_get_returns_new_list(self):
        request_headers = _RequestHeaders({"headers": [(b"key", b"val")]})
        request_headers.get("key").append("mutated")
        self.assertEqual(request_headers.get("key"), ["val"])

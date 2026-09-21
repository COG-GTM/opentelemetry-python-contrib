# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import unittest
from unittest.mock import patch

from opentelemetry.util.http import ExcludeList, get_excluded_urls


class TestGetExcludedUrls(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "OTEL_PYTHON_DJANGO_EXCLUDED_URLS": "excluded_arg/123,excluded_noarg"
        },
    )
    def test_config_from_instrumentation_env(self):
        exclude_list = get_excluded_urls("DJANGO")

        self.assertTrue(exclude_list.url_disabled("/excluded_arg/123"))
        self.assertTrue(exclude_list.url_disabled("/excluded_noarg"))
        self.assertFalse(exclude_list.url_disabled("/excluded_arg/125"))

    @patch.dict(
        "os.environ",
        {"OTEL_PYTHON_EXCLUDED_URLS": "excluded_arg/123,excluded_noarg"},
    )
    def test_config_from_generic_env(self):
        exclude_list = get_excluded_urls("DJANGO")

        self.assertTrue(exclude_list.url_disabled("/excluded_arg/123"))
        self.assertTrue(exclude_list.url_disabled("/excluded_noarg"))
        self.assertFalse(exclude_list.url_disabled("/excluded_arg/125"))

    @patch.dict(
        "os.environ",
        {
            "OTEL_PYTHON_DJANGO_EXCLUDED_URLS": "excluded_arg/123,excluded_noarg",
            "OTEL_PYTHON_EXCLUDED_URLS": "excluded_arg/125",
        },
    )
    def test_config_from_instrumentation_env_takes_precedence(self):
        exclude_list = get_excluded_urls("DJANGO")

        self.assertTrue(exclude_list.url_disabled("/excluded_arg/123"))
        self.assertTrue(exclude_list.url_disabled("/excluded_noarg"))
        self.assertFalse(exclude_list.url_disabled("/excluded_arg/125"))

    @patch.dict(
        "os.environ",
        {
            "OTEL_PYTHON_DJANGO_EXCLUDED_URLS": "",
            "OTEL_PYTHON_EXCLUDED_URLS": "excluded_arg/125",
        },
    )
    def test_config_from_instrumentation_env_empty(self):
        exclude_list = get_excluded_urls("DJANGO")

        self.assertFalse(exclude_list.url_disabled("/excluded_arg/123"))
        self.assertFalse(exclude_list.url_disabled("/excluded_noarg"))
        self.assertFalse(exclude_list.url_disabled("/excluded_arg/125"))


class TestExcludeList(unittest.TestCase):
    def test_query_string_does_not_match(self):
        exclude_list = ExcludeList(["health"])

        self.assertTrue(exclude_list.url_disabled("/health"))
        self.assertTrue(
            exclude_list.url_disabled("http://localhost/health?ready=1")
        )
        self.assertFalse(exclude_list.url_disabled("/admin?x=health"))
        self.assertFalse(
            exclude_list.url_disabled("http://localhost/admin?x=health")
        )

    def test_fragment_does_not_match(self):
        exclude_list = ExcludeList(["health"])

        self.assertFalse(exclude_list.url_disabled("/admin#health"))

    def test_matches_host_and_path(self):
        exclude_list = ExcludeList(["http://localhost/excluded_arg/123"])

        self.assertTrue(
            exclude_list.url_disabled(
                "http://localhost/excluded_arg/123?foo=bar"
            )
        )
        self.assertFalse(
            exclude_list.url_disabled("http://localhost/excluded_arg/125")
        )

    def test_invalid_url_is_matched_as_is(self):
        exclude_list = ExcludeList(["excluded"])

        self.assertTrue(exclude_list.url_disabled("http://[::1/excluded"))

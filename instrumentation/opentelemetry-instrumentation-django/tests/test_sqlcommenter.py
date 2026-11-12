# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# pylint: disable=no-name-in-module
from unittest.mock import MagicMock, patch

import pytest
from django import VERSION, conf
from django.http import HttpResponse
from django.test.utils import setup_test_environment, teardown_test_environment

from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware import (
    SqlCommenter,
    _QueryWrapper,
    _SqlCommenterSettings,
)
from opentelemetry.test.wsgitestutil import WsgiTestBase

DJANGO_2_0 = VERSION >= (2, 0)

_django_instrumentor = DjangoInstrumentor()


class TestMiddleware(WsgiTestBase):
    @classmethod
    def setUpClass(cls):
        conf.settings.configure(
            SQLCOMMENTER_WITH_FRAMEWORK=False,
            SQLCOMMENTER_WITH_DB_DRIVER=False,
        )
        super().setUpClass()

    def setUp(self):
        super().setUp()
        setup_test_environment()
        _django_instrumentor.instrument(is_sql_commentor_enabled=True)

    def tearDown(self):
        super().tearDown()
        teardown_test_environment()
        _django_instrumentor.uninstrument()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        conf.settings = conf.LazySettings()

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware.SqlCommenter"
    )
    def test_middleware_added(self, sqlcommenter_middleware):
        instance = sqlcommenter_middleware.return_value
        instance.get_response = HttpResponse()
        if DJANGO_2_0:
            middleware = conf.settings.MIDDLEWARE
        else:
            middleware = conf.settings.MIDDLEWARE_CLASSES

        self.assertTrue(
            "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware.SqlCommenter"
            in middleware
        )

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware.SqlCommenter"
    )
    def test_middleware_added_at_position(self, sqlcommenter_middleware):
        _django_instrumentor.uninstrument()
        if DJANGO_2_0:
            middleware = conf.settings.MIDDLEWARE
        else:
            middleware = conf.settings.MIDDLEWARE_CLASSES

        # adding two dummy middlewares
        temprory_middelware = "django.utils.deprecation.MiddlewareMixin"
        middleware.append(temprory_middelware)
        middleware.append(temprory_middelware)

        middleware_position = 1
        _django_instrumentor.instrument(
            is_sql_commentor_enabled=True,
            middleware_position=middleware_position,
        )
        instance = sqlcommenter_middleware.return_value
        instance.get_response = HttpResponse()
        self.assertEqual(
            middleware[middleware_position],
            "opentelemetry.instrumentation.django.middleware.otel_middleware._DjangoMiddleware",
        )
        self.assertEqual(
            middleware[middleware_position + 1],
            "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware.SqlCommenter",
        )

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware._get_opentelemetry_values"
    )
    def test_query_wrapper(self, trace_capture):
        requests_mock = MagicMock()
        requests_mock.resolver_match.view_name = "view"
        requests_mock.resolver_match.route = "route"
        requests_mock.resolver_match.app_name = "app"

        trace_capture.return_value = {
            "traceparent": "*traceparent='00-000000000000000000000000deadbeef-000000000000beef-00"
        }
        qw_instance = _QueryWrapper(requests_mock)
        execute_mock_obj = MagicMock()
        qw_instance(
            execute_mock_obj,
            "Select 1;",
            MagicMock("test"),
            MagicMock("test1"),
            MagicMock(),
        )
        output_sql = execute_mock_obj.call_args[0][0]
        self.assertEqual(
            output_sql,
            "Select 1 /*app_name='app',controller='view',route='route',traceparent='%%2Atraceparent%%3D%%2700-0000000"
            "00000000000000000deadbeef-000000000000beef-00'*/;",
        )

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware._get_opentelemetry_values"
    )
    def test_query_wrapper_non_string_queries(self, trace_capture):
        """Test that non-string queries and psycopg2 composable objects are handled correctly."""
        requests_mock = MagicMock()
        requests_mock.resolver_match.view_name = "view"
        requests_mock.resolver_match.route = "route"
        requests_mock.resolver_match.app_name = "app"

        trace_capture.return_value = {
            "traceparent": "*traceparent='00-000000000000000000000000deadbeef-000000000000beef-00"
        }
        qw_instance = _QueryWrapper(requests_mock)
        execute_mock_obj = MagicMock()

        input_query = MagicMock(as_string=lambda conn: "SELECT 2")
        expected_query_start = "SELECT 2"

        qw_instance(
            execute_mock_obj,
            input_query,
            MagicMock("test"),
            MagicMock("test1"),
            MagicMock(),
        )
        output_sql = execute_mock_obj.call_args[0][0]
        self.assertTrue(
            str(output_sql).startswith(str(expected_query_start)),
            f"Query should start with {expected_query_start!r}, got {output_sql!r}",
        )

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware.connections"
    )
    def test_multiple_connection_support(self, connections_mock):
        if not DJANGO_2_0:
            pytest.skip()

        db_connections = {"default": MagicMock(), "other": MagicMock()}
        connections_mock.__iter__.return_value = iter(db_connections)
        connections_mock.__getitem__.side_effect = db_connections.__getitem__

        requests_mock = MagicMock()
        get_response = MagicMock()

        sql_instance = SqlCommenter(get_response)
        sql_instance(requests_mock)

        # check if query_wrapper is added to the context for 2 databases
        wrappers = []
        for connection in db_connections.values():
            connection.execute_wrapper.assert_called_once()
            wrappers.append(connection.execute_wrapper.call_args[0][0])
        self.assertIsInstance(wrappers[0], _QueryWrapper)
        self.assertIs(wrappers[0], wrappers[1])
        self.assertIs(wrappers[0].request, requests_mock)
        get_response.assert_called_once_with(requests_mock)

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware._QueryWrapper"
    )
    def test_settings_read_once_per_request(self, query_wrapper):
        if not DJANGO_2_0:
            pytest.skip()

        requests_mock = MagicMock()
        sql_instance = SqlCommenter(MagicMock())

        with patch.object(
            _SqlCommenterSettings,
            "from_django_settings",
            wraps=_SqlCommenterSettings.from_django_settings,
        ) as from_django_settings:
            sql_instance(requests_mock)

        from_django_settings.assert_called_once_with()
        query_wrapper.assert_called_once_with(
            requests_mock,
            _SqlCommenterSettings(
                with_framework=False,
                with_controller=True,
                with_route=True,
                with_app_name=True,
                with_opentelemetry=True,
                with_db_driver=False,
            ),
        )

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware._get_opentelemetry_values"
    )
    def test_query_wrapper_does_not_read_settings_per_query(
        self, trace_capture
    ):
        requests_mock = MagicMock()
        requests_mock.resolver_match.view_name = "view"
        requests_mock.resolver_match.route = "route"
        requests_mock.resolver_match.app_name = "app"
        trace_capture.return_value = {}

        qw_instance = _QueryWrapper(
            requests_mock,
            _SqlCommenterSettings(
                with_framework=False,
                with_controller=True,
                with_route=False,
                with_app_name=True,
                with_opentelemetry=False,
                with_db_driver=False,
            ),
        )
        execute_mock_obj = MagicMock()
        with patch.object(
            _SqlCommenterSettings, "from_django_settings"
        ) as from_django_settings:
            for _ in range(3):
                qw_instance(
                    execute_mock_obj,
                    "Select 1;",
                    MagicMock("test"),
                    MagicMock("test1"),
                    MagicMock(),
                )

        from_django_settings.assert_not_called()
        trace_capture.assert_not_called()
        self.assertEqual(execute_mock_obj.call_count, 3)
        self.assertEqual(
            execute_mock_obj.call_args[0][0],
            "Select 1 /*app_name='app',controller='view'*/;",
        )

    @patch(
        "opentelemetry.instrumentation.django.middleware.sqlcommenter_middleware._get_opentelemetry_values"
    )
    def test_empty_sql(self, trace_capture):
        requests_mock = MagicMock()
        requests_mock.resolver_match.view_name = "view"
        requests_mock.resolver_match.route = "route"
        requests_mock.resolver_match.app_name = "app"

        trace_capture.return_value = {
            "traceparent": "*traceparent='00-000000000000000000000000deadbeef-000000000000beef-00"
        }
        qw_instance = _QueryWrapper(requests_mock)
        execute_mock_obj = MagicMock()
        qw_instance(
            execute_mock_obj,
            "",
            MagicMock("test"),
            MagicMock("test1"),
            MagicMock(),
        )
        output_sql = execute_mock_obj.call_args[0][0]
        self.assertEqual(
            output_sql,
            " /*app_name='app',controller='view',route='route',traceparent='%%2Atraceparent%%3D%%2700-0000000"
            "00000000000000000deadbeef-000000000000beef-00'*/",
        )

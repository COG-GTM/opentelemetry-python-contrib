# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from contextlib import ExitStack
from logging import getLogger
from typing import Any, NamedTuple, Type, TypeVar

# pylint: disable=no-name-in-module
from django import conf, get_version
from django.db import connections

from opentelemetry.instrumentation.sqlcommenter_utils import _add_sql_comment
from opentelemetry.instrumentation.utils import _get_opentelemetry_values
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

_propagator = TraceContextTextMapPropagator()

_django_version = get_version()
_logger = getLogger(__name__)

T = TypeVar("T")  # pylint: disable-msg=invalid-name


class _SqlCommenterSettings(NamedTuple):
    with_framework: bool
    with_controller: bool
    with_route: bool
    with_app_name: bool
    with_opentelemetry: bool
    with_db_driver: bool

    @classmethod
    def from_django_settings(cls) -> _SqlCommenterSettings:
        settings = conf.settings
        return cls(
            with_framework=getattr(
                settings, "SQLCOMMENTER_WITH_FRAMEWORK", True
            ),
            with_controller=getattr(
                settings, "SQLCOMMENTER_WITH_CONTROLLER", True
            ),
            with_route=getattr(settings, "SQLCOMMENTER_WITH_ROUTE", True),
            with_app_name=getattr(
                settings, "SQLCOMMENTER_WITH_APP_NAME", True
            ),
            with_opentelemetry=getattr(
                settings, "SQLCOMMENTER_WITH_OPENTELEMETRY", True
            ),
            with_db_driver=getattr(
                settings, "SQLCOMMENTER_WITH_DB_DRIVER", True
            ),
        )


class SqlCommenter:
    """
    Middleware to append a comment to each database query with details about
    the framework and the execution context.
    """

    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def __call__(self, request) -> Any:
        # Read once per request (not per query) so override_settings still applies.
        query_wrapper = _QueryWrapper(
            request, _SqlCommenterSettings.from_django_settings()
        )
        with ExitStack() as stack:
            for db_alias in connections:
                stack.enter_context(
                    connections[db_alias].execute_wrapper(query_wrapper)
                )
            return self.get_response(request)


class _QueryWrapper:
    def __init__(
        self, request, settings: _SqlCommenterSettings | None = None
    ) -> None:
        self.request = request
        self.settings = (
            settings
            if settings is not None
            else _SqlCommenterSettings.from_django_settings()
        )

    def __call__(self, execute: Type[T], sql, params, many, context) -> T:
        # pylint: disable-msg=too-many-locals
        (
            with_framework,
            with_controller,
            with_route,
            with_app_name,
            with_opentelemetry,
            with_db_driver,
        ) = self.settings

        db_driver = context["connection"].settings_dict.get("ENGINE", "")
        resolver_match = self.request.resolver_match

        # Convert sql statement to string, handling psycopg2.sql.Composable object
        if hasattr(sql, "as_string"):
            sql = sql.as_string(context["connection"])

        sql = str(sql)

        sql = _add_sql_comment(
            sql,
            # Information about the controller.
            controller=(
                resolver_match.view_name
                if resolver_match and with_controller
                else None
            ),
            # route is the pattern that matched a request with a controller i.e. the regex
            # See https://docs.djangoproject.com/en/stable/ref/urlresolvers/#django.urls.ResolverMatch.route
            # getattr() because the attribute doesn't exist in Django < 2.2.
            route=(
                getattr(resolver_match, "route", None)
                if resolver_match and with_route
                else None
            ),
            # app_name is the application namespace for the URL pattern that matches the URL.
            # See https://docs.djangoproject.com/en/stable/ref/urlresolvers/#django.urls.ResolverMatch.app_name
            app_name=(
                (resolver_match.app_name or None)
                if resolver_match and with_app_name
                else None
            ),
            # Framework centric information.
            framework=f"django:{_django_version}" if with_framework else None,
            # Information about the database and driver.
            db_driver=db_driver if with_db_driver else None,
            **_get_opentelemetry_values() if with_opentelemetry else {},
        )

        return execute(sql, params, many, context)

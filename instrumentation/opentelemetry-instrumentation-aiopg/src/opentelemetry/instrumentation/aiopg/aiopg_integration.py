# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
import typing
from collections.abc import Coroutine

import wrapt

from opentelemetry.instrumentation._semconv import _report_new
from opentelemetry.instrumentation.dbapi import (
    CursorTracer,
    DatabaseApiIntegration,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import SpanKind


# pylint: disable=abstract-method,no-member
class AsyncProxyObject(wrapt.ObjectProxy):
    def __aiter__(self):
        return self.__wrapped__.__aiter__()

    async def __anext__(self):
        result = await self.__wrapped__.__anext__()
        return result

    async def __aenter__(self):
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.__wrapped__.__aexit__(exc_type, exc_val, exc_tb)

    def __await__(self):
        return self.__wrapped__.__await__()


class AiopgIntegration(DatabaseApiIntegration):
    async def wrapped_connection(
        self,
        connect_method: typing.Callable[..., typing.Any],
        args: typing.Tuple[typing.Any, typing.Any],
        kwargs: typing.Dict[typing.Any, typing.Any],
    ):
        """Add object proxy to connection object."""
        connection = await connect_method(*args, **kwargs)
        # pylint: disable=protected-access
        self.get_connection_attributes(connection._conn)
        return get_traced_connection_proxy(connection, self)

    async def wrapped_pool(self, create_pool_method, args, kwargs):
        pool = await create_pool_method(*args, **kwargs)
        async with pool.acquire() as connection:
            # pylint: disable=protected-access
            self.get_connection_attributes(connection._conn)
        return get_traced_pool_proxy(pool, self)


# pylint: disable=abstract-method,no-member
class TracedConnectionProxy(AsyncProxyObject):
    _self_db_api_integration: DatabaseApiIntegration

    # pylint: disable=unused-argument
    def __init__(
        self,
        connection: typing.Any,
        db_api_integration: DatabaseApiIntegration,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(connection)
        self._self_db_api_integration = db_api_integration

    def cursor(
        self, *args: typing.Any, **kwargs: typing.Any
    ) -> _ContextManager:
        coro = self._cursor(*args, **kwargs)
        return _ContextManager(coro)

    async def _cursor(
        self, *args: typing.Any, **kwargs: typing.Any
    ) -> AsyncCursorTracerProxy:
        # pylint: disable=protected-access
        cursor = await self.__wrapped__._cursor(*args, **kwargs)
        return get_traced_cursor_proxy(cursor, self._self_db_api_integration)


def get_traced_connection_proxy(
    connection: typing.Any,
    db_api_integration: DatabaseApiIntegration,
    *args: typing.Any,
    **kwargs: typing.Any,
) -> TracedConnectionProxy:
    return TracedConnectionProxy(
        connection, db_api_integration, *args, **kwargs
    )


# pylint: disable=abstract-method,no-member
class TracedPoolProxy(AsyncProxyObject):
    _self_db_api_integration: DatabaseApiIntegration
    _self_args: tuple[typing.Any, ...]
    _self_kwargs: dict[str, typing.Any]

    # pylint: disable=unused-argument
    def __init__(
        self,
        pool: typing.Any,
        db_api_integration: DatabaseApiIntegration,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(pool)
        self._self_db_api_integration = db_api_integration
        self._self_args = args
        self._self_kwargs = kwargs

    def acquire(self) -> _PoolAcquireContextManager:
        """Acquire free connection from the pool."""
        coro = self._acquire()
        return _PoolAcquireContextManager(coro, self)

    async def _acquire(self) -> typing.Any:
        # pylint: disable=protected-access
        connection = await self.__wrapped__._acquire()
        if not isinstance(connection, AsyncProxyObject):
            connection = get_traced_connection_proxy(
                connection,
                self._self_db_api_integration,
                *self._self_args,
                **self._self_kwargs,
            )
        return connection


def get_traced_pool_proxy(
    pool: typing.Any,
    db_api_integration: DatabaseApiIntegration,
    *args: typing.Any,
    **kwargs: typing.Any,
) -> TracedPoolProxy:
    return TracedPoolProxy(pool, db_api_integration, *args, **kwargs)


class AsyncCursorTracer(CursorTracer):
    async def traced_execution(
        self,
        cursor,
        query_method: typing.Callable[..., typing.Any],
        *args: typing.Tuple[typing.Any, typing.Any],
        **kwargs: typing.Dict[typing.Any, typing.Any],
    ):
        name = ""
        if args:
            name = self.get_operation_name(cursor, args)

        if not name:
            name = (
                self._db_api_integration.database
                if self._db_api_integration.database
                else self._db_api_integration.name
            )

        with self._db_api_integration._tracer.start_as_current_span(
            name, kind=SpanKind.CLIENT
        ) as span:
            if span.is_recording():
                self._populate_span(span, cursor, *args)
            try:
                return await query_method(*args, **kwargs)
            except Exception as exc:
                if span.is_recording() and _report_new(
                    self._db_api_integration._sem_conv_opt_in_mode_db
                ):
                    span.set_attribute(ERROR_TYPE, type(exc).__qualname__)
                raise


# pylint: disable=abstract-method,no-member
class AsyncCursorTracerProxy(AsyncProxyObject):
    _self_cursor_tracer: AsyncCursorTracer

    # pylint: disable=unused-argument
    def __init__(
        self,
        cursor: typing.Any,
        db_api_integration: DatabaseApiIntegration,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(cursor)
        self._self_cursor_tracer = AsyncCursorTracer(db_api_integration)

    async def execute(
        self, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        result = await self._self_cursor_tracer.traced_execution(
            self, self.__wrapped__.execute, *args, **kwargs
        )
        return result

    async def executemany(
        self, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        result = await self._self_cursor_tracer.traced_execution(
            self, self.__wrapped__.executemany, *args, **kwargs
        )
        return result

    async def callproc(
        self, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        result = await self._self_cursor_tracer.traced_execution(
            self, self.__wrapped__.callproc, *args, **kwargs
        )
        return result


def get_traced_cursor_proxy(
    cursor: typing.Any,
    db_api_integration: DatabaseApiIntegration,
    *args: typing.Any,
    **kwargs: typing.Any,
) -> AsyncCursorTracerProxy:
    return AsyncCursorTracerProxy(cursor, db_api_integration, *args, **kwargs)


class _ContextManager(Coroutine):
    __slots__ = ("_coro", "_obj")

    def __init__(self, coro):
        self._coro = coro
        self._obj = None

    def send(self, value):
        return self._coro.send(value)

    def throw(self, typ, val=None, tb=None):
        if val is None:
            return self._coro.throw(typ)
        if tb is None:
            return self._coro.throw(typ, val)
        return self._coro.throw(typ, val, tb)

    def close(self):
        return self._coro.close()

    @property
    def gi_frame(self):
        return self._coro.gi_frame

    @property
    def gi_running(self):
        return self._coro.gi_running

    @property
    def gi_code(self):
        return self._coro.gi_code

    def __next__(self):
        return self.send(None)

    def __await__(self):
        resp = self._coro.__await__()
        return resp

    async def __aenter__(self):
        self._obj = await self._coro
        return self._obj

    async def __aexit__(self, exc_type, exc, t_b):
        try:
            if inspect.iscoroutinefunction(self._obj.close):
                await self._obj.close()
            else:
                self._obj.close()
        finally:
            self._obj = None


class _PoolContextManager(_ContextManager):
    __slots__ = ()

    async def __aexit__(self, exc_type, exc, tb):
        self._obj.close()
        await self._obj.wait_closed()
        self._obj = None


class _PoolAcquireContextManager(_ContextManager):
    # pylint: disable=redefined-slots-in-subclass
    __slots__ = ("_coro", "_obj", "_pool")

    def __init__(self, coro, pool):
        super().__init__(coro)
        self._pool = pool

    async def __aexit__(self, exc_type, exc, tb):
        await self._pool.release(self._obj)
        self._pool = None
        self._obj = None

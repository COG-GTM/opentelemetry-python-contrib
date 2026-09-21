# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

"""
Instrument `asyncclick`_ CLI applications. The instrumentor will avoid instrumenting
well-known servers (e.g. *flask run* and *uvicorn*) to avoid unexpected effects
like every request having the same Trace ID.



.. _asyncclick: https://pypi.org/project/asyncclick/

Usage
-----

.. code-block:: python

    import asyncio
    import asyncclick
    from opentelemetry.instrumentation.asyncclick import AsyncClickInstrumentor

    AsyncClickInstrumentor().instrument()

    @asyncclick.command()
    async def hello():
        asyncclick.echo(f'Hello world!')

    if __name__ == "__main__":
        asyncio.run(hello())

The command line recorded in ``process.command_args`` is redacted: values of
options whose name looks sensitive (for example ``--password``, ``--token`` or
``--api-key``) and credentials embedded in URL arguments are replaced with
``REDACTED``.

API
---
"""

from __future__ import annotations

import os
import re
import sys
from functools import partial
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Collection,
    Sequence,
    TypeVar,
)
from urllib.parse import urlparse, urlunparse

import asyncclick
from typing_extensions import ParamSpec, Unpack
from wrapt import (
    wrap_function_wrapper,  # type: ignore[reportUnknownVariableType]
)

from opentelemetry import trace
from opentelemetry.instrumentation.asyncclick.package import _instruments
from opentelemetry.instrumentation.asyncclick.version import __version__
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import (
    unwrap,
)
from opentelemetry.semconv._incubating.attributes.process_attributes import (
    PROCESS_COMMAND_ARGS,
    PROCESS_EXECUTABLE_NAME,
    PROCESS_EXIT_CODE,
    PROCESS_PID,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace.status import StatusCode

if TYPE_CHECKING:
    from typing import TypedDict

    class InstrumentKwargs(TypedDict, total=False):
        tracer_provider: trace.TracerProvider

    class UninstrumentKwargs(TypedDict, total=False):
        pass


_logger = getLogger(__name__)


T = TypeVar("T")
P = ParamSpec("P")

_REDACTED = "REDACTED"
_SENSITIVE_OPTION_RE = re.compile(
    r"pass(word|wd)?|secret|token|credential|api[-_]?key|^-{1,2}key$", re.I
)


def _is_sensitive_option(arg: str) -> bool:
    return arg.startswith("-") and bool(_SENSITIVE_OPTION_RE.search(arg))


def _remove_url_credentials(value: str) -> str:
    """Replace the userinfo of a valid URL with the keyword `REDACTED`."""
    try:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc and "@" in parsed.netloc:
            _, _, host = parsed.netloc.rpartition("@")
            return urlunparse(
                (
                    parsed.scheme,
                    f"{_REDACTED}:{_REDACTED}@{host}",
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
    except ValueError:
        pass
    return value


def _redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Mask credentials passed on the command line.

    Values of options whose name looks sensitive (``--password``, ``--token``,
    ...) are replaced by ``REDACTED``, and credentials embedded in URL
    arguments are stripped.
    """
    redacted: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            redacted.append(_REDACTED)
            mask_next = False
        elif arg.startswith("-") and "=" in arg:
            name, _, value = arg.partition("=")
            if _is_sensitive_option(name):
                redacted.append(f"{name}={_REDACTED}")
            else:
                redacted.append(f"{name}={_remove_url_credentials(value)}")
        elif _is_sensitive_option(arg):
            redacted.append(arg)
            mask_next = True
        else:
            redacted.append(_remove_url_credentials(arg))
    return tuple(redacted)


async def _command_invoke_wrapper(
    wrapped: Callable[P, Awaitable[T]],
    instance: asyncclick.core.Command,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tracer: trace.Tracer,
) -> T:
    # Subclasses of Command include groups and CLI runners, but
    # we only want to instrument the actual commands which are
    # instances of Command itself.
    if instance.__class__ != asyncclick.Command:
        return await wrapped(*args, **kwargs)

    ctx = args[0]

    span_name = ctx.info_name
    span_attributes = {
        PROCESS_COMMAND_ARGS: _redact_argv(sys.argv),
        PROCESS_EXECUTABLE_NAME: sys.argv[0],
        PROCESS_EXIT_CODE: 0,
        PROCESS_PID: os.getpid(),
    }

    with tracer.start_as_current_span(
        name=span_name,
        kind=trace.SpanKind.INTERNAL,
        attributes=span_attributes,
    ) as span:
        try:
            result = await wrapped(*args, **kwargs)
            return result
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            if span.is_recording():
                span.set_attribute(ERROR_TYPE, type(exc).__qualname__)
                span.set_attribute(
                    PROCESS_EXIT_CODE, getattr(exc, "exit_code", 1)
                )
            raise


# pylint: disable=no-self-use
class AsyncClickInstrumentor(BaseInstrumentor):
    """An instrumentor for asyncclick"""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Unpack[InstrumentKwargs]) -> None:
        tracer_provider = kwargs.get("tracer_provider")
        tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider,
        )

        wrap_function_wrapper(
            asyncclick.core.Command,
            "invoke",
            partial(_command_invoke_wrapper, tracer=tracer),
        )

    def _uninstrument(self, **kwargs: Unpack["UninstrumentKwargs"]) -> None:
        unwrap(asyncclick.core.Command, "invoke")

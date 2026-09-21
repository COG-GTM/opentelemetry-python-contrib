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
    Mapping,
    NamedTuple,
    Sequence,
    TypeVar,
)

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
from opentelemetry.util.http import redact_url

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
_SENSITIVE_OPTION_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "credentials",
        "key",
        "keys",
        "pass",
        "passphrase",
        "passwd",
        "password",
        "pwd",
        "secret",
        "secrets",
        "token",
    }
)
# Names that are sensitive even when written without separators, such as
# ``--apikey`` or ``--clientsecret``.
_SENSITIVE_OPTION_SUBSTRINGS = frozenset(
    {
        "apikey",
        "authorization",
        "credential",
        "passphrase",
        "passwd",
        "password",
        "secret",
        "token",
    }
)


def _is_sensitive_option(option: str) -> bool:
    name = option.lstrip("-").casefold()
    tokens = re.split(r"[-_.]", name)
    if not _SENSITIVE_OPTION_TOKENS.isdisjoint(tokens):
        return True
    return any(substring in name for substring in _SENSITIVE_OPTION_SUBSTRINGS)


class _OptionInfo(NamedTuple):
    sensitive: bool
    takes_value: bool
    nargs: int


def _declared_options(
    ctx: asyncclick.Context,
) -> dict[str, _OptionInfo]:
    """Describe every option spelling declared by the command.

    The command and its parent groups are inspected so that short aliases
    (``-p`` for ``--password``) are known too. When a spelling is declared
    more than once in the chain, the declaration leading to the most redaction
    wins, so a nested non-sensitive flag cannot unmask a parent's secret.
    """
    options: dict[str, _OptionInfo] = {}
    node: asyncclick.Context | None = ctx
    while node is not None:
        for param in node.command.params:
            if not isinstance(param, asyncclick.Option):
                continue
            spellings = [*param.opts, *param.secondary_opts]
            takes_value = not (param.is_flag or param.count)
            info = _OptionInfo(
                sensitive=any(
                    _is_sensitive_option(spelling)
                    for spelling in (param.name or "", *spellings)
                ),
                takes_value=takes_value,
                nargs=param.nargs if takes_value else 0,
            )
            for spelling in spellings:
                current = options.get(spelling)
                if current is None or _redaction_rank(info) > _redaction_rank(
                    current
                ):
                    options[spelling] = info
        node = node.parent
    return options


def _redaction_rank(info: _OptionInfo) -> tuple[bool, int]:
    return (info.sensitive and info.takes_value, info.nargs)


def _redact_argv(
    argv: Sequence[str],
    options: Mapping[str, _OptionInfo] | None = None,
) -> tuple[str, ...]:
    """Redact secrets from command line arguments.

    Values of options declared as sensitive by the command (``options``) or
    whose name looks sensitive (``--password``, ``--token``, ...) are replaced
    by ``REDACTED``, in the ``--opt=value``, ``--opt value``, ``-Ovalue`` and
    ``-fOvalue`` (short option cluster) forms, once per value the option
    consumes. Options known to be flags are left untouched, since they carry no
    value. Remaining arguments go through
    :func:`opentelemetry.util.http.redact_url` so that credentials embedded in
    URLs (such as database DSNs) are redacted as well.
    """
    declared = options or {}
    redacted: list[str] = []
    pending = 0
    for arg in argv:
        if pending:
            redacted.append(_REDACTED)
            pending -= 1
            continue
        if arg.startswith("-"):
            option, separator, _ = arg.partition("=")
            info = declared.get(option)
            if info is None and _is_sensitive_option(option):
                info = _OptionInfo(sensitive=True, takes_value=True, nargs=1)
            if info is not None:
                if not (info.sensitive and info.takes_value):
                    redacted.append(arg)
                elif separator:
                    redacted.append(f"{option}={_REDACTED}")
                    pending = info.nargs - 1
                else:
                    redacted.append(arg)
                    pending = info.nargs
                continue
            cluster = _redact_short_cluster(arg, declared)
            if cluster is not None:
                arg, pending = cluster
                redacted.append(arg)
                continue
        redacted.append(redact_url(arg))
    return tuple(redacted)


def _redact_short_cluster(
    arg: str,
    options: Mapping[str, _OptionInfo],
) -> tuple[str, int] | None:
    """Redact a sensitive short option inside a cluster such as ``-vpsecret``.

    Returns the redacted argument and the number of following arguments that
    still hold values of the option, or ``None`` when the cluster holds no
    sensitive option consuming a value.
    """
    if arg.startswith("--") or len(arg) < 3:
        return None
    for index in range(1, len(arg)):
        info = options.get(f"-{arg[index]}")
        if info is None:
            return None
        if not info.takes_value:
            continue
        if not info.sensitive:
            return None
        if index + 1 < len(arg):
            return f"{arg[: index + 1]}{_REDACTED}", info.nargs - 1
        return arg, info.nargs
    return None


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
        PROCESS_COMMAND_ARGS: _redact_argv(sys.argv, _declared_options(ctx)),
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

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from re import compile as re_compile
from typing import Any, Dict, Iterable, Optional

from opentelemetry.baggage import get_all, set_baggage
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.trace import (
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    get_current_span,
    set_span_in_context,
)

OT_TRACE_ID_HEADER = "ot-tracer-traceid"
OT_SPAN_ID_HEADER = "ot-tracer-spanid"
OT_SAMPLED_HEADER = "ot-tracer-sampled"
OT_BAGGAGE_PREFIX = "ot-baggage-"

# Limits equivalent to those of the W3C baggage specification, applied to
# baggage extracted from inbound headers.
_MAX_BAGGAGE_ENTRIES = 180
_MAX_ENTRY_LENGTH = 4096
_MAX_TOTAL_LENGTH = 8192

_valid_header_name = re_compile(r"[\w_^`!#$%&'*+.|~]+")
_valid_header_value = re_compile(r"[\t\x20-\x7e\x80-\xff]+")
_valid_extract_traceid = re_compile(r"[0-9a-f]{1,32}")
_valid_extract_spanid = re_compile(r"[0-9a-f]{1,16}")


class OTTracePropagator(TextMapPropagator):
    """Propagator for the OTTrace HTTP header format"""

    def extract(
        self,
        carrier: CarrierT,
        context: Optional[Context] = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        if context is None:
            context = Context()

        traceid = _extract_identifier(
            getter.get(carrier, OT_TRACE_ID_HEADER),
            _valid_extract_traceid,
            INVALID_TRACE_ID,
        )

        spanid = _extract_identifier(
            getter.get(carrier, OT_SPAN_ID_HEADER),
            _valid_extract_spanid,
            INVALID_SPAN_ID,
        )

        sampled = _extract_first_element(
            getter.get(carrier, OT_SAMPLED_HEADER)
        )

        if sampled == "true":
            traceflags = TraceFlags.SAMPLED
        else:
            traceflags = TraceFlags.DEFAULT

        if traceid != INVALID_TRACE_ID and spanid != INVALID_SPAN_ID:
            context = set_span_in_context(
                NonRecordingSpan(
                    SpanContext(
                        trace_id=traceid,
                        span_id=spanid,
                        is_remote=True,
                        trace_flags=TraceFlags(traceflags),
                    )
                ),
                context,
            )

            baggage = dict(get_all(context) or {})
            baggage.update(_extract_baggage(carrier, getter))

            for key, value in baggage.items():
                context = set_baggage(key, value, context)

        return context

    def inject(
        self,
        carrier: CarrierT,
        context: Optional[Context] = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        span_context = get_current_span(context).get_span_context()

        if span_context.trace_id == INVALID_TRACE_ID:
            return

        setter.set(
            carrier, OT_TRACE_ID_HEADER, hex(span_context.trace_id)[2:][-16:]
        )
        setter.set(
            carrier,
            OT_SPAN_ID_HEADER,
            hex(span_context.span_id)[2:][-16:],
        )

        if span_context.trace_flags == TraceFlags.SAMPLED:
            traceflags = "true"
        else:
            traceflags = "false"

        setter.set(carrier, OT_SAMPLED_HEADER, traceflags)

        baggage = get_all(context)

        if not baggage:
            return

        for header_name, header_value in baggage.items():
            if (
                _valid_header_name.fullmatch(header_name) is None
                or _valid_header_value.fullmatch(header_value) is None
            ):
                continue

            setter.set(
                carrier,
                "".join([OT_BAGGAGE_PREFIX, header_name]),
                header_value,
            )

    @property
    def fields(self):
        """Returns a set with the fields set in `inject`.

        See
        `opentelemetry.propagators.textmap.TextMapPropagator.fields`
        """
        return {
            OT_TRACE_ID_HEADER,
            OT_SPAN_ID_HEADER,
            OT_SAMPLED_HEADER,
        }


def _extract_first_element(
    items: Iterable[CarrierT],
    default: Any = None,
) -> Optional[CarrierT]:
    if items is None:
        return default
    return next(iter(items), None)


def _extract_baggage(
    carrier: CarrierT, getter: Getter[CarrierT]
) -> Dict[str, str]:
    """Extract ``ot-baggage-*`` headers, dropping invalid or oversized entries.

    Entries are bounded in number, per entry length and total length so that a
    remote caller cannot force arbitrary amounts of baggage into the context.
    """
    baggage: Dict[str, str] = {}
    total_length = 0

    for header in getter.keys(carrier):
        if not header.startswith(OT_BAGGAGE_PREFIX):
            continue

        if len(baggage) >= _MAX_BAGGAGE_ENTRIES:
            break

        key = header[len(OT_BAGGAGE_PREFIX) :]
        value = _extract_first_element(getter.get(carrier, header))

        if not isinstance(value, str):
            continue

        if (
            _valid_header_name.fullmatch(key) is None
            or _valid_header_value.fullmatch(value) is None
        ):
            continue

        entry_length = len(key) + len(value)

        if entry_length > _MAX_ENTRY_LENGTH:
            continue

        if total_length + entry_length > _MAX_TOTAL_LENGTH:
            break

        total_length += entry_length
        baggage[key] = value

    return baggage


def _extract_identifier(
    items: Iterable[CarrierT], validator_pattern, default: int
) -> int:
    header = _extract_first_element(items)
    if header is None or validator_pattern.fullmatch(header) is None:
        return default

    try:
        return int(header, 16)
    except ValueError:
        return default

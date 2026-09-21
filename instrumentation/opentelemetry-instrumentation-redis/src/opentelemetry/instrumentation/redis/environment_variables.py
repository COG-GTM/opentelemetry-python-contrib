# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

OTEL_INSTRUMENTATION_REDIS_CAPTURE_SEARCH_CONTENT = (
    "OTEL_INSTRUMENTATION_REDIS_CAPTURE_SEARCH_CONTENT"
)
"""
.. envvar:: OTEL_INSTRUMENTATION_REDIS_CAPTURE_SEARCH_CONTENT

Opt in to capturing the ``FT.SEARCH`` query text and the field values of the
returned documents as span attributes. Disabled by default because that
content may contain sensitive data.
"""

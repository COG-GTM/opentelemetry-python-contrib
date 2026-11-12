# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import itertools
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from opentelemetry import context
from opentelemetry.instrumentation.utils import _url_quote

if sys.version_info >= (3, 14):
    from string.templatelib import Template as _Template
else:
    _Template = ()

if TYPE_CHECKING:
    if sys.version_info >= (3, 14):
        from string.templatelib import Template
    else:
        from typing import Never

        Template = Never
    from typing import overload

    @overload
    def _add_sql_comment(sql: str, **meta: Any) -> str: ...

    @overload
    def _add_sql_comment(sql: Template, **meta: Any) -> Template: ...


def _add_sql_comment(sql: str | Template, **meta: Any) -> str | Template:
    """
    Appends comments to the sql statement and returns it
    """
    framework_tags = _add_framework_tags()
    if framework_tags:
        meta.update(framework_tags)
    comment = _generate_sql_comment(**meta)

    if isinstance(sql, _Template):
        last = sql.strings[-1].rstrip()
        if last.endswith(";"):
            last = last[:-1] + comment + ";"
        else:
            last += comment
        args = [
            *itertools.chain.from_iterable(
                zip(sql.strings[:-1], sql.interpolations)
            ),
            last,
        ]
        return _Template(*args)

    sql = sql.rstrip()
    if sql.endswith(";"):
        sql = sql[:-1] + comment + ";"
    else:
        sql = sql + comment
    return sql


def _generate_sql_comment(**meta) -> str:
    """
    Return a SQL comment with comma delimited key=value pairs created from
    **meta kwargs.
    """
    key_value_delimiter = ","

    if not meta:  # No entries added.
        return ""

    # Sort the keywords to ensure that caching works and that testing is
    # deterministic. It eases visual inspection as well.
    return (
        " /*"
        + key_value_delimiter.join(
            _comment_fragment(key, value)
            for key, value in sorted(meta.items())
            if value is not None
        )
        + "*/"
    )


def _comment_fragment(key: Any, value: Any) -> str:
    # Most values (driver name, dbapi level, ...) repeat on every query, so
    # the quoted fragment is memoized; only exact str is cached to keep the
    # cache keyed by plain, immutable, hashable values.
    if type(key) is str and type(value) is str:
        return _cached_str_comment_fragment(key, value)
    return f"{_url_quote(key)}={_url_quote(value)!r}"


@lru_cache(maxsize=256)
def _cached_str_comment_fragment(key: str, value: str) -> str:
    return f"{_url_quote(key)}={_url_quote(value)!r}"


def _add_framework_tags() -> dict:
    """
    Returns orm related tags if any set by the context
    """
    return context.get_value("SQLCOMMENTER_ORM_TAGS_AND_VALUES") or {}

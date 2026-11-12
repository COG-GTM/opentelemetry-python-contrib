# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import gc
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from google.genai.types import GenerateContentConfig

from opentelemetry.instrumentation._semconv import (
    _OpenTelemetrySemanticConventionStability,
    _OpenTelemetryStabilitySignalType,
    _StabilityMode,
)
from opentelemetry.instrumentation.google_genai import generate_content
from opentelemetry.instrumentation.google_genai.generate_content import (
    GEN_AI_TOOL_DEFINITIONS,
    _GenerateContentInstrumentationHelper,
)
from opentelemetry.util.genai.types import FunctionToolDefinition

from .base import TestCase
from .nonstreaming_base import _is_mcp_imported, _mock_mcp_client_session


class _FakeMcpTool:
    def __init__(self, name: str):
        self.name = name

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {
            "name": self.name,
            "description": f"{self.name} description",
            "inputSchema": {"type": "object"},
        }


class _FakeMcpSession:
    def __init__(self, *tool_names: str, started=None, release=None):
        self._tool_names = tool_names
        self._started = started
        self._release = release
        self.list_tools_calls = 0

    async def list_tools(self):
        self.list_tools_calls += 1
        if self._started is not None:
            self._started.append(self)
        if self._release is not None:
            await self._release.wait()
        return SimpleNamespace(
            tools=[_FakeMcpTool(name) for name in self._tool_names]
        )


def _function_tool(name: str) -> FunctionToolDefinition:
    return FunctionToolDefinition(
        name=name,
        description=f"{name} description",
        parameters={"type": "object"},
    )


def _some_callable_tool():
    """A callable tool."""


class TestMcpSessionToolDefinitions(unittest.TestCase):
    def setUp(self):
        mcp_patches = [
            patch.object(generate_content, "_is_mcp_imported", True),
            patch.object(
                generate_content, "McpClientSession", _FakeMcpSession
            ),
            patch.object(
                generate_content,
                "_config_to_tools",
                lambda config: config,
            ),
        ]
        for mcp_patch in mcp_patches:
            mcp_patch.start()
            self.addCleanup(mcp_patch.stop)

    @staticmethod
    def _helper(
        experimental_sem_convs_enabled: bool = True,
    ) -> _GenerateContentInstrumentationHelper:
        helper = _GenerateContentInstrumentationHelper.__new__(
            _GenerateContentInstrumentationHelper
        )
        helper.experimental_sem_convs_enabled = experimental_sem_convs_enabled
        return helper

    def _get_tool_definitions(self, tools, **kwargs):
        return asyncio.run(
            self._helper(**kwargs)._maybe_get_tool_definitions_async(tools)
        )

    def test_lists_each_session_once_across_calls(self):
        session = _FakeMcpSession("search", "fetch")

        first = self._get_tool_definitions([session])
        second = self._get_tool_definitions([session])

        expected = [_function_tool("search"), _function_tool("fetch")]
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(session.list_tools_calls, 1)

    def test_sessions_are_cached_independently(self):
        session_a = _FakeMcpSession("a")
        session_b = _FakeMcpSession("b")

        self.assertEqual(
            self._get_tool_definitions([session_a]), [_function_tool("a")]
        )
        self.assertEqual(
            self._get_tool_definitions([session_b]), [_function_tool("b")]
        )
        self.assertEqual(
            self._get_tool_definitions([session_b, session_a]),
            [_function_tool("b"), _function_tool("a")],
        )
        self.assertEqual(session_a.list_tools_calls, 1)
        self.assertEqual(session_b.list_tools_calls, 1)

    def test_lists_uncached_sessions_concurrently_and_keeps_order(self):
        async def run():
            started = []
            release = asyncio.Event()
            sessions = [
                _FakeMcpSession("first", started=started, release=release),
                _FakeMcpSession("second", started=started, release=release),
            ]
            task = asyncio.ensure_future(
                self._helper()._maybe_get_tool_definitions_async(
                    [sessions[0], _some_callable_tool, sessions[1]]
                )
            )
            for _ in range(10):
                if len(started) == len(sessions):
                    break
                await asyncio.sleep(0)
            self.assertEqual(started, sessions)
            release.set()
            return await asyncio.wait_for(task, timeout=5)

        self.assertEqual(
            asyncio.run(run()),
            [
                _function_tool("first"),
                FunctionToolDefinition(
                    name="_some_callable_tool",
                    description="A callable tool.",
                    parameters=None,
                ),
                _function_tool("second"),
            ],
        )

    def test_returned_definitions_do_not_alias_cache(self):
        session = _FakeMcpSession("search")

        first = self._get_tool_definitions([session])
        first[0].description = "mutated"
        first.clear()

        self.assertEqual(
            self._get_tool_definitions([session]), [_function_tool("search")]
        )

    def test_cache_entry_is_released_with_session(self):
        session = _FakeMcpSession("search")
        self._get_tool_definitions([session])
        self.assertIn(session, generate_content._mcp_session_tool_definitions)

        session_ref = weakref.ref(session)
        del session
        gc.collect()

        self.assertIsNone(session_ref())

    def test_does_not_list_tools_without_experimental_sem_convs(self):
        session = _FakeMcpSession("search")

        self.assertEqual(
            self._get_tool_definitions(
                [session], experimental_sem_convs_enabled=False
            ),
            [],
        )
        self.assertEqual(session.list_tools_calls, 0)


@unittest.skipUnless(_is_mcp_imported, "mcp is not installed")
class TestAsyncGenerateContentMcpSessionToolDefinitions(TestCase):
    def setUp(self):
        super().setUp()
        env_patch = patch.dict(
            "os.environ",
            {
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
            },
        )
        mapping_patch = patch.dict(
            _OpenTelemetrySemanticConventionStability._OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING,
            {
                _OpenTelemetryStabilitySignalType.GEN_AI: _StabilityMode.GEN_AI_LATEST_EXPERIMENTAL
            },
        )
        for p in (env_patch, mapping_patch):
            p.start()
            self.addCleanup(p.stop)

    def _assert_tool_definitions_on_each_call(self, generate):
        session = _mock_mcp_client_session()
        config = GenerateContentConfig(tools=[session])
        for _ in range(3):
            self.configure_valid_response(text="ok")
            asyncio.run(generate(config))

        self.assertEqual(session.list_tools.await_count, 1)
        events = self.otel.get_events_named(
            "gen_ai.client.inference.operation.details"
        )
        self.assertEqual(len(events), 3)
        for event in events:
            (tool_definition,) = event.attributes[GEN_AI_TOOL_DEFINITIONS]
            self.assertEqual(tool_definition["name"], "mcp_tool")
            self.assertEqual(
                tool_definition["description"], "Tool from session"
            )
            self.assertEqual(tool_definition["type"], "function")

    def test_generate_content_lists_session_tools_once(self):
        async def generate(config):
            return await self.client.aio.models.generate_content(
                model="gemini-2.0-flash", contents="Hi", config=config
            )

        self._assert_tool_definitions_on_each_call(generate)

    def test_generate_content_stream_lists_session_tools_once(self):
        async def generate(config):
            return [
                response
                async for response in await self.client.aio.models.generate_content_stream(
                    model="gemini-2.0-flash", contents="Hi", config=config
                )
            ]

        self._assert_tool_definitions_on_each_call(generate)

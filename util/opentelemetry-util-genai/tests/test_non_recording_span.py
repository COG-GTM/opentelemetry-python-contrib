# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Sampler
from opentelemetry.semconv._incubating.attributes import (
    gen_ai_attributes as GenAI,
)
from opentelemetry.semconv.attributes import error_attributes
from opentelemetry.util.genai import _invocation, _workflow_invocation
from opentelemetry.util.genai.handler import TelemetryHandler
from opentelemetry.util.genai.types import (
    Error,
    FunctionToolDefinition,
    InputMessage,
    OutputMessage,
    Text,
)
from opentelemetry.util.genai.utils import gen_ai_json_dumps

from .test_utils import patch_env_vars


class _BoomError(RuntimeError):
    pass


class TestNonRecordingSpan(unittest.TestCase):
    """Span-only attributes are skipped for unsampled spans, while metrics,
    events and the completion hook are still produced."""

    def _make_handler(self, sampler: Sampler) -> TelemetryHandler:
        self.span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider(sampler=sampler)
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(self.span_exporter)
        )
        self.metric_reader = InMemoryMetricReader()
        self.log_exporter = InMemoryLogRecordExporter()
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(
            SimpleLogRecordProcessor(self.log_exporter)
        )
        self.completion_hook = MagicMock()
        return TelemetryHandler(
            tracer_provider=tracer_provider,
            meter_provider=MeterProvider(metric_readers=[self.metric_reader]),
            logger_provider=logger_provider,
            completion_hook=self.completion_hook,
        )

    def _duration_point_attributes(self) -> list[dict[str, Any]]:
        data = self.metric_reader.get_metrics_data()
        assert data is not None
        return [
            dict(point.attributes or {})
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
            if metric.name == "gen_ai.client.operation.duration"
            for point in metric.data.data_points
        ]

    @staticmethod
    def _input() -> list[InputMessage]:
        return [InputMessage(role="user", parts=[Text(content="hello")])]

    @staticmethod
    def _output() -> list[OutputMessage]:
        return [
            OutputMessage(
                role="assistant",
                parts=[Text(content="hi")],
                finish_reason="stop",
            )
        ]

    @staticmethod
    def _tools() -> list[FunctionToolDefinition]:
        return [
            FunctionToolDefinition(
                name="get_weather",
                description="Get the weather",
                parameters={"type": "object"},
            )
        ]

    def _run_inference(self, handler: TelemetryHandler) -> None:
        invocation = handler.start_inference("prov", request_model="model")
        invocation.input_messages = self._input()
        invocation.output_messages = self._output()
        invocation.tool_definitions = self._tools()
        invocation.input_tokens = 3
        invocation.output_tokens = 4
        invocation.stop()

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_AND_EVENT",
        emit_event="true",
    )
    def test_inference_unsampled_skips_span_content_serialization(self):
        handler = self._make_handler(ALWAYS_OFF)
        with patch.object(
            _invocation, "gen_ai_json_dumps", wraps=gen_ai_json_dumps
        ) as dumps:
            self._run_inference(handler)

        dumps.assert_not_called()
        self.assertEqual(self.span_exporter.get_finished_spans(), ())
        self.assertEqual(len(self._duration_point_attributes()), 1)

        logs = self.log_exporter.get_finished_logs()
        self.assertEqual(len(logs), 1)
        event_attrs = logs[0].log_record.attributes
        assert event_attrs is not None
        self.assertIn(GenAI.GEN_AI_INPUT_MESSAGES, event_attrs)
        self.assertIn(GenAI.GEN_AI_OUTPUT_MESSAGES, event_attrs)

        self.completion_hook.on_completion.assert_called_once()
        kwargs = self.completion_hook.on_completion.call_args.kwargs
        self.assertEqual(kwargs["inputs"], self._input())
        self.assertEqual(kwargs["outputs"], self._output())
        self.assertIs(kwargs["log_record"], logs[0].log_record)

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_AND_EVENT",
        emit_event="true",
    )
    def test_inference_sampled_still_sets_span_content(self):
        handler = self._make_handler(ALWAYS_ON)
        self._run_inference(handler)

        spans = self.span_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        attrs = spans[0].attributes
        assert attrs is not None
        for key in (
            GenAI.GEN_AI_INPUT_MESSAGES,
            GenAI.GEN_AI_OUTPUT_MESSAGES,
            GenAI.GEN_AI_TOOL_DEFINITIONS,
        ):
            self.assertIsInstance(attrs[key], str)
        self.assertEqual(attrs[GenAI.GEN_AI_USAGE_INPUT_TOKENS], 3)
        self.assertEqual(attrs[GenAI.GEN_AI_USAGE_OUTPUT_TOKENS], 4)

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="NO_CONTENT",
        emit_event="",
    )
    def test_inference_unsampled_skips_tool_definitions_without_content(
        self,
    ):
        handler = self._make_handler(ALWAYS_OFF)
        with patch.object(
            _invocation, "gen_ai_json_dumps", wraps=gen_ai_json_dumps
        ) as dumps:
            self._run_inference(handler)
        dumps.assert_not_called()

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_ONLY",
        emit_event="",
    )
    def test_inference_unsampled_fail_records_error_metric(self):
        handler = self._make_handler(ALWAYS_OFF)
        invocation = handler.start_inference("prov", request_model="model")
        invocation.input_messages = self._input()
        invocation.fail(Error(message="boom", type=_BoomError))

        points = self._duration_point_attributes()
        self.assertEqual(len(points), 1)
        self.assertEqual(
            points[0][error_attributes.ERROR_TYPE], _BoomError.__qualname__
        )

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_ONLY",
        emit_event="",
    )
    def test_agent_unsampled_skips_span_content_serialization(self):
        handler = self._make_handler(ALWAYS_OFF)
        with patch.object(
            _invocation, "gen_ai_json_dumps", wraps=gen_ai_json_dumps
        ) as dumps:
            invocation = handler.start_invoke_local_agent(
                "prov", request_model="model", agent_name="agent"
            )
            invocation.input_messages = self._input()
            invocation.output_messages = self._output()
            invocation.tool_definitions = self._tools()
            invocation.fail(Error(message="boom", type=_BoomError))

        dumps.assert_not_called()
        self.assertEqual(self.span_exporter.get_finished_spans(), ())
        points = self._duration_point_attributes()
        self.assertEqual(len(points), 1)
        self.assertEqual(
            points[0][error_attributes.ERROR_TYPE], _BoomError.__qualname__
        )
        self.completion_hook.on_completion.assert_called_once()

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_ONLY",
        emit_event="",
    )
    def test_agent_sampled_still_sets_span_content(self):
        handler = self._make_handler(ALWAYS_ON)
        invocation = handler.start_invoke_local_agent(
            "prov", request_model="model", agent_name="agent"
        )
        invocation.input_messages = self._input()
        invocation.output_messages = self._output()
        invocation.stop()

        spans = self.span_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        attrs = spans[0].attributes
        assert attrs is not None
        self.assertIsInstance(attrs[GenAI.GEN_AI_INPUT_MESSAGES], str)
        self.assertIsInstance(attrs[GenAI.GEN_AI_OUTPUT_MESSAGES], str)
        self.assertEqual(attrs[GenAI.GEN_AI_AGENT_NAME], "agent")

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_ONLY",
        emit_event="",
    )
    def test_workflow_unsampled_skips_span_content_serialization(self):
        handler = self._make_handler(ALWAYS_OFF)
        with patch.object(
            _workflow_invocation,
            "gen_ai_json_dumps",
            wraps=gen_ai_json_dumps,
        ) as dumps:
            invocation = handler.start_workflow(name="pipeline")
            invocation.input_messages = self._input()
            invocation.output_messages = self._output()
            invocation.stop()

        dumps.assert_not_called()
        self.assertEqual(self.span_exporter.get_finished_spans(), ())
        self.completion_hook.on_completion.assert_called_once()

    @patch_env_vars(
        stability_mode="gen_ai_latest_experimental",
        content_capturing="SPAN_ONLY",
        emit_event="",
    )
    def test_workflow_sampled_still_sets_span_content(self):
        handler = self._make_handler(ALWAYS_ON)
        invocation = handler.start_workflow(name="pipeline")
        invocation.input_messages = self._input()
        invocation.fail(Error(message="boom", type=_BoomError))

        spans = self.span_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        attrs = spans[0].attributes
        assert attrs is not None
        self.assertIsInstance(attrs[GenAI.GEN_AI_INPUT_MESSAGES], str)
        self.assertEqual(
            attrs[error_attributes.ERROR_TYPE], _BoomError.__qualname__
        )

    def test_tool_unsampled_fail_records_error_metric(self):
        handler = self._make_handler(ALWAYS_OFF)
        invocation = handler.start_tool("get_weather", arguments={"q": 1})
        with patch.object(invocation.span, "set_attributes") as set_attrs:
            invocation.fail(Error(message="boom", type=_BoomError))

        set_attrs.assert_not_called()
        self.assertEqual(self.span_exporter.get_finished_spans(), ())
        points = self._duration_point_attributes()
        self.assertEqual(len(points), 1)
        self.assertEqual(
            points[0][error_attributes.ERROR_TYPE], _BoomError.__qualname__
        )

    def test_embedding_unsampled_fail_records_error_metric(self):
        handler = self._make_handler(ALWAYS_OFF)
        invocation = handler.start_embedding("prov", request_model="model")
        with patch.object(invocation.span, "set_attributes") as set_attrs:
            invocation.fail(Error(message="boom", type=_BoomError))

        set_attrs.assert_not_called()
        self.assertEqual(self.span_exporter.get_finished_spans(), ())
        points = self._duration_point_attributes()
        self.assertEqual(len(points), 1)
        self.assertEqual(
            points[0][error_attributes.ERROR_TYPE], _BoomError.__qualname__
        )

    def test_embedding_sampled_fail_sets_error_type_on_span(self):
        handler = self._make_handler(ALWAYS_ON)
        invocation = handler.start_embedding("prov", request_model="model")
        invocation.fail(Error(message="boom", type=_BoomError))

        spans = self.span_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        attrs = spans[0].attributes
        assert attrs is not None
        self.assertEqual(
            attrs[error_attributes.ERROR_TYPE], _BoomError.__qualname__
        )
        self.assertEqual(attrs[GenAI.GEN_AI_REQUEST_MODEL], "model")

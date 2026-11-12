# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest
from rich.text import Text
from rich.tree import Tree

import opentelemetry.trace
from opentelemetry.exporter.richconsole import RichConsoleSpanExporter
from opentelemetry.sdk import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor


@pytest.fixture(name="span_processor")
def fixture_span_processor():
    exporter = RichConsoleSpanExporter()
    span_processor = BatchSpanProcessor(exporter)

    yield span_processor

    span_processor.shutdown()


@pytest.fixture(name="tracer_provider")
def fixture_tracer_provider(span_processor):
    tracer_provider = trace.TracerProvider()
    tracer_provider.add_span_processor(span_processor)

    yield tracer_provider


def test_span_exporter(tracer_provider, span_processor, capsys):
    tracer = tracer_provider.get_tracer(__name__)
    span = tracer.start_span("test_span")

    span.set_attribute("key", "V4LuE")
    span.end()
    span_processor.force_flush()
    captured = capsys.readouterr()

    assert "V4LuE" in captured.out


def walk_tree(root: Tree) -> int:
    # counts the amount of spans in a tree that contains a span
    return sum(walk_tree(child) for child in root.children) + int(
        "span" in root.label
    )


def test_multiple_traces(tracer_provider):
    exporter = RichConsoleSpanExporter()
    tracer = tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("parent_1") as parent_1:
        with tracer.start_as_current_span("child_1") as child_1:
            pass

    with tracer.start_as_current_span("parent_2") as parent_2:
        pass

    trees = exporter.spans_to_tree((parent_2, parent_1, child_1))
    # asserts that we have all traces
    assert len(trees) == 2
    traceid_1 = opentelemetry.trace.format_trace_id(parent_1.context.trace_id)

    assert traceid_1 in trees

    assert (
        opentelemetry.trace.format_trace_id(parent_2.context.trace_id) in trees
    )

    # asserts that we have exactly the number of spans we exported
    assert sum(walk_tree(tree) for tree in trees.values()) == 3

    # assert that the relationship is correct
    assert parent_1.name in trees[traceid_1].children[0].label
    assert any(
        child_1.name in child.label
        for child in trees[traceid_1].children[0].children
    )
    assert not any(
        parent_1.name in child.label
        for child in trees[traceid_1].children[0].children
    )
    assert not any(
        parent_2.name in child.label
        for child in trees[traceid_1].children[0].children
    )


@pytest.mark.timeout(30)
def test_no_deadlock(tracer_provider):
    # non-regression test for https://github.com/open-telemetry/opentelemetry-python-contrib/issues/3254

    tracer = tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("parent"):
        with tracer.start_as_current_span("child") as child:
            pass

    RichConsoleSpanExporter.spans_to_tree((child,))


def test_suppress_resource(span_processor):
    attributes = {"resource.key": "resource.value"}
    resource = Resource(attributes)
    tracer_provider = trace.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(span_processor)
    tracer = tracer_provider.get_tracer(__name__)

    with tracer.start_as_current_span("parent") as parent:
        with tracer.start_as_current_span("child") as child:
            pass

    trees = RichConsoleSpanExporter.spans_to_tree(
        (parent, child), suppress_resource=True
    )
    assert len(trees) == 1

    nodes = [next(t for t in trees.values())]
    for node in nodes:
        label = node.label
        if isinstance(label, Text):
            label = label.plain

        assert "resource" not in label.lower()

        nodes.extend(node.children)


def _span_children(node: Tree) -> list[Tree]:
    return [
        child
        for child in node.children
        if isinstance(child.label, Text) and ", span " in child.label.plain
    ]


def _span_name(node: Tree) -> str:
    return node.label.plain.split("] ", 1)[1].split(",", 1)[0]


def _child_names(node: Tree) -> list[str]:
    return [_span_name(child) for child in _span_children(node)]


def test_children_before_parents(
    tracer_provider: trace.TracerProvider,
) -> None:
    tracer = tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("root") as root:
        with tracer.start_as_current_span("a") as span_a:
            with tracer.start_as_current_span("a1") as span_a1:
                with tracer.start_as_current_span("a1x") as span_a1x:
                    pass
        with tracer.start_as_current_span("b") as span_b:
            pass

    trees = RichConsoleSpanExporter.spans_to_tree(
        (span_a1x, span_a1, span_b, span_a, root)
    )

    assert len(trees) == 1
    tree = next(iter(trees.values()))
    assert walk_tree(tree) == 5
    assert _child_names(tree) == ["root"]
    root_node = _span_children(tree)[0]
    assert _child_names(root_node) == ["b", "a"]
    node_a = _span_children(root_node)[1]
    assert _child_names(node_a) == ["a1"]
    assert _child_names(_span_children(node_a)[0]) == ["a1x"]


def test_siblings_keep_batch_order(
    tracer_provider: trace.TracerProvider,
) -> None:
    tracer = tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("parent") as parent:
        siblings = []
        for index in range(6):
            with tracer.start_as_current_span(f"child_{index}") as child:
                siblings.append(child)

    trees = RichConsoleSpanExporter.spans_to_tree((*siblings, parent))

    tree = next(iter(trees.values()))
    assert _child_names(_span_children(tree)[0]) == [
        f"child_{index}" for index in range(6)
    ]


def test_roots_keep_batch_order(
    tracer_provider: trace.TracerProvider,
) -> None:
    tracer = tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("parent"):
        orphans = []
        for index in range(6):
            with tracer.start_as_current_span(f"orphan_{index}") as span:
                orphans.append(span)

    trees = RichConsoleSpanExporter.spans_to_tree(orphans)

    assert _child_names(next(iter(trees.values()))) == [
        f"orphan_{index}" for index in range(6)
    ]


@pytest.mark.timeout(30)
def test_deep_trace_reversed_batch() -> None:
    tracer = trace.TracerProvider().get_tracer(__name__)
    root = tracer.start_span("root")
    spans = [root]
    for _ in range(1000):
        spans.append(
            tracer.start_span(
                "deep",
                context=opentelemetry.trace.set_span_in_context(spans[-1]),
            )
        )
    for span in spans:
        span.end()

    trees = RichConsoleSpanExporter.spans_to_tree(spans[::-1])

    assert len(trees) == 1
    node = next(iter(trees.values()))
    depth = 0
    while children := _span_children(node):
        assert len(children) == 1
        node = children[0]
        depth += 1
    assert depth == 1001

"""轨迹评估测试（T-P2-04）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from evaluation.trajectory_eval import (
    CASES,
    evaluate_trajectories,
    extract_path,
    phase_of,
)


def _span(name: str, start: int, **attrs):
    return SimpleNamespace(name=name, start_time=start, attributes=attrs)


def test_phase_mapping():
    assert phase_of("supervisor") == "route"
    assert phase_of("supervisor_synthesize") == "synthesize"
    assert phase_of("knowledge_rag_process") == "knowledge"
    assert phase_of("rag_generate") == "knowledge"
    assert phase_of("ticket_handler_process") == "ticket"
    assert phase_of("compliance_process") == "compliance"
    assert phase_of("compliance_gate") == "gate"


def test_extract_path_dedupes_and_orders():
    spans = [
        _span("invoke_agent supervisor.route_decision", 1, **{"gen_ai.agent.name": "supervisor"}),
        _span("invoke_agent knowledge_rag.rewrite_query", 2, **{"gen_ai.agent.name": "rag_query_rewrite"}),
        _span("invoke_agent knowledge_rag.process", 3, **{"gen_ai.agent.name": "knowledge_rag_process"}),
        _span("execute_tool knowledge_search", 4, **{"gen_ai.tool.name": "knowledge_search"}),
        _span("invoke_agent compliance.process", 5, **{"gen_ai.agent.name": "compliance_process"}),
        _span("invoke_agent supervisor.synthesize", 6, **{"gen_ai.agent.name": "supervisor_synthesize"}),
    ]
    nodes, tools = extract_path(spans)
    assert nodes == ["route", "knowledge", "compliance", "synthesize"]
    assert tools == ["knowledge_search"]


def test_out_of_order_spans_are_sorted():
    spans = [
        _span("invoke_agent supervisor.synthesize", 30, **{"gen_ai.agent.name": "supervisor_synthesize"}),
        _span("invoke_agent supervisor.route", 10, **{"gen_ai.agent.name": "supervisor"}),
    ]
    nodes, _ = extract_path(spans)
    assert nodes == ["route", "synthesize"]


def test_cases_have_consistent_shape():
    assert CASES and all(isinstance(c.expected_nodes, tuple) for c in CASES)
    assert any(c.should_block for c in CASES)


def test_demo_graph_trajectory_matches_expectation():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from evaluation.trajectory_eval import _build_demo_graph
    from tracing.otel_config import install_provider, reset_tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    install_provider(provider)
    try:
        graph = _build_demo_graph()
        report = asyncio.run(evaluate_trajectories(graph, exporter=exporter))
    finally:
        reset_tracing()

    assert report.total == len(CASES)
    assert report.accuracy == 1.0, report.to_dict()

def test_unified_report_includes_trajectory():
    from evaluation.run import DEFAULT_THRESHOLDS, _check, build_report

    report = asyncio.run(build_report(with_trajectory=True))
    assert report["trajectory"] is not None
    assert report["trajectory"]["accuracy"] == 1.0
    assert _check(report, DEFAULT_THRESHOLDS) == []

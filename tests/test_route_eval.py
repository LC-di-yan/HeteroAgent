"""路由评估与统一评估入口测试（T-P2-04 / T-P2-05）。"""
from __future__ import annotations

import asyncio

from evaluation.route_eval import (
    KNOWLEDGE,
    ROUTE_CASES,
    TICKET,
    evaluate_routes,
    keyword_baseline_router,
    make_llm_router,
)
from evaluation.run import DEFAULT_THRESHOLDS, Thresholds, _check, _human_summary, build_report
from tests.fakes import StructuredFakeLLM


def test_route_cases_cover_both_classes():
    labels = {c.expected for c in ROUTE_CASES}
    assert labels == {KNOWLEDGE, TICKET}
    assert len(ROUTE_CASES) >= 25


def test_baseline_router_is_imperfect_but_reasonable():
    report = asyncio.run(evaluate_routes(keyword_baseline_router))
    # 基线不应满分（否则评测没有区分度），也不应低于门禁
    assert 0.75 <= report.accuracy < 1.0
    assert report.total == len(ROUTE_CASES)


def test_confusion_matrix_is_consistent():
    report = asyncio.run(evaluate_routes(keyword_baseline_router))
    total = sum(sum(row.values()) for row in report.confusion.values())
    assert total == report.total
    assert report.correct + len(report.failures) == report.total


def test_perfect_router_scores_one():
    async def perfect(query: str) -> str:
        for case in ROUTE_CASES:
            if case.query == query:
                return case.expected
        return KNOWLEDGE

    report = asyncio.run(evaluate_routes(perfect))
    assert report.accuracy == 1.0
    assert report.failures == []


def test_llm_router_uses_structured_output():
    from agents.schemas import RouteDecision

    llm = StructuredFakeLLM([RouteDecision(target="ticket_handler")])
    router = make_llm_router(llm)
    assert asyncio.run(router("我要退款")) == TICKET


def test_router_error_is_counted_as_failure():
    async def broken(query: str) -> str:
        raise RuntimeError("boom")

    report = asyncio.run(evaluate_routes(broken, ROUTE_CASES[:3]))
    assert report.accuracy == 0.0
    assert len(report.failures) == 3


def test_unified_report_contains_sections():
    report = asyncio.run(build_report())
    assert {"route", "compliance", "retrieval", "thresholds"} <= set(report)
    assert report["route"]["total"] >= 25
    assert report["compliance"]["confusion"]["fn"] == 0


def test_gate_passes_on_current_baseline():
    report = asyncio.run(build_report())
    assert _check(report, DEFAULT_THRESHOLDS) == []


def test_gate_flags_route_regression():
    report = asyncio.run(build_report())
    report["route"]["accuracy"] = 0.5
    violations = _check(report, DEFAULT_THRESHOLDS)
    assert any("路由准确率" in v for v in violations)


def test_gate_flags_compliance_leak():
    report = asyncio.run(build_report())
    report["compliance"]["confusion"]["fn"] = 2
    violations = _check(report, Thresholds())
    assert any("漏放" in v for v in violations)


def test_human_summary_contains_numbers():
    report = asyncio.run(build_report())
    text = _human_summary(report, [])
    assert "路由" in text and "合规" in text and "通过" in text

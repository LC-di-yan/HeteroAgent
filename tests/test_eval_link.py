"""评估结果回挂 trace 测试（T-P2-04）。"""
from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tracing.eval_link import (
    ATTR_DATASET,
    ATTR_NAME,
    ATTR_SCORE,
    EVAL_SPAN_NAME,
    extract_metrics,
    record_evaluation,
    record_report,
)
from tracing.otel_config import install_provider, reset_tracing


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    install_provider(provider)
    return exporter


def test_record_evaluation_creates_span():
    exporter = _exporter()
    try:
        assert record_evaluation(name="route_accuracy", score=0.87, dataset="v1") is True
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == EVAL_SPAN_NAME
        assert span.attributes[ATTR_NAME] == "route_accuracy"
        assert span.attributes[ATTR_SCORE] == 0.87
        assert span.attributes[ATTR_DATASET] == "v1"
    finally:
        reset_tracing()


def test_extract_metrics_from_report():
    report = {
        "route": {"accuracy": 0.9, "total": 30},
        "compliance": {"metrics": {"precision": 1.0, "recall": 1.0, "false_positive_rate": 0.0}},
        "trajectory": {"accuracy": 1.0, "total": 3},
        "retrieval": {"metrics": {"recall@5": 0.8, "mrr": 0.7}},
    }
    names = [name for name, _, _ in extract_metrics(report)]
    assert "route_accuracy" in names
    assert "trajectory_accuracy" in names
    assert "retrieval_recall_at_5" in names
    assert "retrieval_mrr" in names


def test_record_report_writes_all_metrics():
    exporter = _exporter()
    try:
        report = {
            "route": {"accuracy": 0.88, "total": 30},
            "compliance": {"metrics": {"precision": 1.0}},
            "trajectory": {"accuracy": 1.0, "total": 3},
        }
        written = record_report(report, dataset="v1")
        assert written == 3
        assert len(exporter.get_finished_spans()) == 3
    finally:
        reset_tracing()


def test_empty_report_writes_nothing():
    exporter = _exporter()
    try:
        assert record_report({}) == 0
        assert exporter.get_finished_spans() == ()
    finally:
        reset_tracing()

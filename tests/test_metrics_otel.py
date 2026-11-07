"""OTel Metrics 测试（T-P2-07）。"""
from __future__ import annotations

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tracing.metrics_otel import OtelMetricsBridge, setup_metrics
from tracing.otel_config import AgentMetrics


def _names(reader) -> set[str]:
    data = reader.get_metrics_data()
    if data is None:
        return set()
    names = set()
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                names.add(metric.name)
    return names


def test_setup_returns_none_without_endpoint_or_reader():
    assert setup_metrics(otlp_endpoint="") is None


def test_bridge_records_call_metrics():
    reader = InMemoryMetricReader()
    bridge = setup_metrics(service_name="t", reader=reader)
    assert isinstance(bridge, OtelMetricsBridge)

    bridge.record_call("supervisor", 120.0, True)
    bridge.record_call("supervisor", 80.0, False)

    names = _names(reader)
    assert "heteroagent.agent.calls" in names
    assert "heteroagent.agent.duration" in names
    assert "heteroagent.agent.errors" in names


def test_bridge_records_tokens_and_cost():
    reader = InMemoryMetricReader()
    bridge = setup_metrics(service_name="t", reader=reader)
    bridge.record_tokens("gpt-5.1", 100, 50, 0.002)
    names = _names(reader)
    assert "heteroagent.llm.tokens" in names
    assert "heteroagent.llm.cost" in names


class _SpyBridge:
    def __init__(self):
        self.calls = []
        self.tokens = []

    def record_call(self, agent, duration_ms, success):
        self.calls.append((agent, duration_ms, success))

    def record_tokens(self, model, input_tokens, output_tokens, cost_usd=0.0):
        self.tokens.append((model, input_tokens, output_tokens, cost_usd))


def test_agent_metrics_forward_to_bridge():
    spy = _SpyBridge()
    metrics = AgentMetrics()
    metrics.attach_otel(spy)

    metrics.record_call("knowledge_rag", 300.0, True)
    metrics.record_tokens("gpt-5.1", 10, 5)

    assert spy.calls == [("knowledge_rag", 300.0, True)]
    assert spy.tokens and spy.tokens[0][1] == 10
    # 进程内统计仍然有效
    assert metrics.get_summary()["knowledge_rag"]["total_calls"] == 1


def test_bridge_failure_does_not_break_metrics():
    class Broken:
        def record_call(self, *args, **kwargs):
            raise RuntimeError("otel down")

        def record_tokens(self, *args, **kwargs):
            raise RuntimeError("otel down")

    metrics = AgentMetrics()
    metrics.attach_otel(Broken())
    metrics.record_call("x", 10.0, True)   # 不应抛出
    assert metrics.get_summary()["x"]["total_calls"] == 1


def test_detach_bridge():
    spy = _SpyBridge()
    metrics = AgentMetrics()
    metrics.attach_otel(spy)
    metrics.attach_otel(None)
    metrics.record_call("x", 1.0, True)
    assert spy.calls == []

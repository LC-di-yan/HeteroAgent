"""Prometheus 导出测试（T-P1-08）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import create_app
from config import Settings
from security.approvals import MemoryApprovalStore
from tracing.metrics_export import render_prometheus
from tracing.otel_config import AgentMetrics


class StubSTM:
    async def add_message(self, sid, role, content):
        return None

    async def get_history(self, sid):
        return []


class StubLTM:
    def __init__(self):
        self._documents = []

    def add_document(self, content, source="", metadata=None):
        return "id"


def _metrics() -> AgentMetrics:
    m = AgentMetrics()
    m.record_call("supervisor", 120.0, True)
    m.record_call("supervisor", 80.0, False)
    m.record_call("knowledge_rag", 300.0, True)
    return m


def test_render_contains_expected_series():
    text = render_prometheus(_metrics(), version="0.3.0")
    assert "# TYPE heteroagent_agent_calls_total counter" in text
    assert 'heteroagent_agent_calls_total{agent="supervisor"} 2' in text
    assert 'heteroagent_agent_errors_total{agent="supervisor"} 1' in text
    assert 'heteroagent_agent_duration_ms{agent="knowledge_rag",quantile="0.95"}' in text
    assert 'heteroagent_build_info{version="0.3.0"} 1' in text
    assert text.endswith("\n")


def test_label_escaping():
    m = AgentMetrics()
    m.record_call('weird"agent\nname', 10.0, True)
    text = render_prometheus(m)
    assert 'agent="weird\\"agent\\nname"' in text


def test_empty_metrics_still_valid():
    text = render_prometheus(AgentMetrics())
    assert "# TYPE heteroagent_agent_calls_total counter" in text
    assert "heteroagent_cost_usd_total 0" in text


def _client(**overrides):
    base = dict(require_auth=False, api_keys="", rate_limit_per_minute=1000)
    base.update(overrides)
    st = Settings(**base)
    app = create_app(settings=st, graph=None, short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    app.state.metrics = _metrics()
    return TestClient(app)


def test_metrics_endpoint_returns_text():
    client = _client()
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "heteroagent_agent_calls_total" in resp.text


def test_metrics_endpoint_requires_auth_when_not_public():
    client = _client(require_auth=True, api_keys="secret")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"X-API-Key": "secret"})
    assert ok.status_code == 200


def test_metrics_endpoint_public_flag():
    client = _client(metrics_public=True, require_auth=True, api_keys="secret")
    assert client.get("/metrics").status_code == 200

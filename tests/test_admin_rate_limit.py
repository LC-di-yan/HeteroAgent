"""管理端点限流测试（T-P0-07 收口）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import create_app
from config import Settings
from security.approvals import MemoryApprovalStore


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


def _client(per_minute: int) -> TestClient:
    st = Settings(require_auth=False, api_keys="", rate_limit_per_minute=per_minute)
    app = create_app(settings=st, graph=None, short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    return TestClient(app)


def test_approval_endpoints_are_rate_limited():
    client = _client(per_minute=2)
    assert client.get("/api/approvals").status_code == 200
    assert client.get("/api/approvals").status_code == 200
    assert client.get("/api/approvals").status_code == 429


def test_history_and_metrics_share_the_same_limiter():
    client = _client(per_minute=2)
    assert client.get("/api/history/s1").status_code == 200   # 第 1 次
    assert client.get("/api/metrics").status_code == 200      # 第 2 次（同一配额桶）
    assert client.get("/api/approvals").status_code == 429    # 第 3 次 → 超限


def test_chat_still_limited_too():
    client = _client(per_minute=1)
    first = client.post("/api/chat", json={"message": "hi"})
    second = client.post("/api/chat", json={"message": "hi"})
    assert first.status_code in (503, 200)
    assert second.status_code == 429

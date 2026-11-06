"""用量预算测试（T-P3-04）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import create_app
from config import Settings
from security.approvals import MemoryApprovalStore
from security.budget import TenantBudget


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


class StubGraph:
    async def ainvoke(self, state, config=None):
        return {"final_response": "答案", "intent": "knowledge_rag",
                "compliance_passed": True, "sub_results": {}}


def test_unlimited_budget_always_allows():
    budget = TenantBudget()
    status = budget.consume("u1")
    assert status.allowed is True
    assert status.remaining_requests is None


def test_request_limit_enforced():
    budget = TenantBudget(daily_requests=2)
    assert budget.consume("u1").allowed is True
    assert budget.consume("u1").allowed is True
    third = budget.consume("u1")
    assert third.allowed is False
    assert third.reason == "请求数超限"
    assert third.remaining_requests == 0


def test_subjects_are_isolated():
    budget = TenantBudget(daily_requests=1)
    assert budget.consume("u1").allowed is True
    assert budget.consume("u1").allowed is False
    assert budget.consume("u2").allowed is True


def test_token_budget_via_add_tokens():
    budget = TenantBudget(daily_tokens=100)
    budget.consume("u1")
    budget.add_tokens("u1", 60)
    status = budget.add_tokens("u1", 50)
    assert status.allowed is False          # 110 > 100
    assert status.reason == "token 超限"
    assert status.remaining_tokens == 0


def test_daily_rollover_resets(monkeypatch):
    clock = {"now": 1_700_000_000.0}
    budget = TenantBudget(daily_requests=1, clock=lambda: clock["now"])
    assert budget.consume("u1").allowed is True
    assert budget.consume("u1").allowed is False
    clock["now"] += 86400  # 次日
    assert budget.consume("u1").allowed is True


def test_reset_at_is_next_midnight():
    clock = {"now": 1_700_000_000.0}
    budget = TenantBudget(daily_requests=1, clock=lambda: clock["now"])
    status = budget.consume("u1")
    assert status.reset_at % 86400 == 0
    assert status.reset_at > clock["now"]


def test_headers_include_remaining():
    budget = TenantBudget(daily_requests=5, daily_tokens=1000)
    status = budget.consume("u1")
    headers = status.headers()
    assert headers["X-Budget-Remaining-Requests"] == "4"
    assert headers["X-Budget-Remaining-Tokens"] == "1000"
    assert "X-Budget-Reset" in headers


def test_api_rejects_after_quota():
    st = Settings(require_auth=False, api_keys="", rate_limit_per_minute=1000,
                  tenant_daily_requests=1, checkpoint_backend="memory")
    app = create_app(settings=st, graph=StubGraph(), short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    client = TestClient(app)

    first = client.post("/api/chat", json={"message": "hi", "user_id": "quota-user"})
    assert first.status_code == 200
    second = client.post("/api/chat", json={"message": "hi", "user_id": "quota-user"})
    assert second.status_code == 429
    assert "X-Budget-Remaining-Requests" in second.headers
    # 换一个用户不受影响
    other = client.post("/api/chat", json={"message": "hi", "user_id": "other-user"})
    assert other.status_code == 200

"""缓存命中在 API 响应中的呈现（T-P3-04）。"""
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


class CachedGraph:
    async def ainvoke(self, state, config=None):
        return {
            "final_response": "缓存答案", "intent": "knowledge_rag", "compliance_passed": True,
            "sub_results": {"knowledge_rag": "缓存答案", "cache": {"hit": True, "match": "exact"}},
        }


def test_cache_hit_flag_in_response():
    st = Settings(require_auth=False, api_keys="", rate_limit_per_minute=1000)
    app = create_app(settings=st, graph=CachedGraph(), short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    client = TestClient(app)
    body = client.post("/api/chat", json={"message": "收益多少", "user_id": "u1"}).json()
    assert body["cache_hit"] is True

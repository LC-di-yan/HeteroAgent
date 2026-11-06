"""端到端：真实 LangGraph 拓扑 + 假 LLM + 桩向量库（离线）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agents.supervisor import create_supervisor_graph
from api.main import create_app
from config import Settings
from security.approvals import MemoryApprovalStore
from tests.conftest import FakeLLM

ANSWER_OK = '{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}'
ANSWER_BAD = '{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}'


class StubMemory:
    """模拟向量库：返回固定高分文档。"""

    def __init__(self, docs=None):
        self._documents: list = []
        self.docs = docs or [{"id": "d1", "content": "理财产品A年化收益率为3.5%-5.2%",
                              "source": "product_faq.md", "score": 0.92}]
        self.asearch_calls = 0

    def add_document(self, content, source="", metadata=None):
        self._documents.append({"content": content})
        return "id"

    def search(self, query, top_k=5):
        return self.docs[:top_k]

    async def asearch(self, query, top_k=5):
        self.asearch_calls += 1
        return self.docs[:top_k]


class StubSTM:
    def __init__(self):
        self.messages: dict[str, list] = {}

    async def add_message(self, sid, role, content):
        self.messages.setdefault(sid, []).append({"role": role, "content": content})

    async def get_history(self, sid):
        return list(self.messages.get(sid, []))


def _client(responses: list[str], settings: Settings | None = None):
    st = settings or Settings(require_auth=False, api_keys="", rate_limit_per_minute=1000)
    llm = FakeLLM(responses)
    memory = StubMemory()
    graph = create_supervisor_graph(llm=llm, long_term_memory=memory, settings=st,
                                    enable_checkpointing=False)
    app = create_app(settings=st, graph=graph, llm=llm, short_term_memory=StubSTM(),
                     long_term_memory=memory, approvals=MemoryApprovalStore())
    return TestClient(app), memory, llm


def test_e2e_knowledge_path_success():
    # 顺序：route -> rewrite -> generate -> self_correct -> compliance_llm_check
    client, memory, llm = _client([
        "knowledge_rag",                       # 路由
        "理财产品A 年化收益率",                 # query 改写
        "理财产品A年化收益率为3.5%-5.2%。来源：product_faq.md",  # 生成
        "PASS",                                # 自检
        ANSWER_OK,                             # 合规 LLM 审查
    ])
    resp = client.post("/api/chat", json={"message": "理财产品A收益多少", "user_id": "u1", "session_id": "s1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["compliance_passed"] is True
    assert "3.5%" in body["response"]
    assert body["intent"] == "knowledge_rag"
    assert memory.asearch_calls == 1  # 走异步检索（T-P0-10），未阻塞


def test_e2e_compliance_block_creates_approval():
    client, _, _ = _client([
        "knowledge_rag",
        "理财收益",
        "我们保证收益，零风险，稳赚不赔。",        # 违规回答
        "PASS",
        ANSWER_OK,
    ])
    resp = client.post("/api/chat", json={"message": "你们保证收益吗", "user_id": "u1", "session_id": "s2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["compliance_passed"] is False
    assert body["approval_id"]
    assert "人工审核" in body["response"]

    # 审批放行：违规词必须被清洗，不能回流给用户
    approved = client.post(f"/api/approvals/{body['approval_id']}/approve", json={"reason": "ok"}).json()
    assert "保证收益" not in approved["response"]
    assert "[已屏蔽]" in approved["response"]


def test_e2e_ticket_path():
    client, _, _ = _client([
        "ticket_handler",
        '{"action": "create", "ticket_type": "refund", "priority": "high",'
        ' "summary": "退款申请", "details": "7 天内退款"}',
        ANSWER_OK,
    ])
    resp = client.post("/api/chat", json={"message": "我要申请退款", "user_id": "u1", "session_id": "s3"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "ticket_handler"
    assert "工单号" in body["response"]

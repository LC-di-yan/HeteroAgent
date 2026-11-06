"""离线演示模式测试（DEMO_MODE）。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from agents.demo_mode import DemoKnowledgeBase, ScriptedChatModel
from api.main import create_app
from config import Settings
from security.approvals import MemoryApprovalStore


class _Sys:
    """用 SystemMessage 触发分派逻辑。"""


def _msgs(system: str, human: str):
    from langchain_core.messages import SystemMessage

    return [SystemMessage(content=system), HumanMessage(content=human)]


def test_scripted_router_choice():
    model = ScriptedChatModel()
    assert model._reply(_msgs("你是 Supervisor", "我要退款")) == "ticket_handler"
    assert model._reply(_msgs("你是 Supervisor", "收益多少")) == "knowledge_rag"


def test_scripted_compliance_verdict():
    model = ScriptedChatModel()
    good = model._reply(_msgs("你是金融合规审查Agent", "正常回答"))
    bad = model._reply(_msgs("你是金融合规审查Agent", "我们保证收益"))
    assert '"passed": true' in good
    assert '"passed": false' in bad


def test_scripted_ticket_json_is_valid():
    import json

    model = ScriptedChatModel()
    raw = model._reply(_msgs("你是工单处理Agent，请以JSON格式返回", "我要退款"))
    data = json.loads(raw)
    assert data["action"] == "create" and data["ticket_type"] == "refund"


def test_demo_kb_scores_are_normalised():
    kb = DemoKnowledgeBase()
    hits = kb.search("理财产品A的收益率")
    assert hits and hits[0]["source"] == "product_faq.md"
    assert all(0.0 <= h["score"] <= 1.0 for h in hits)
    assert kb.search("完全无关的问题xyz") == []


def test_full_chain_in_demo_mode_without_api_key():
    st = Settings(demo_mode=True, require_auth=False, api_keys="",
                  rate_limit_per_minute=1000, checkpoint_backend="memory")
    app = create_app(settings=st, approvals=MemoryApprovalStore())

    with TestClient(app) as client:  # 上下文管理器才会触发 lifespan（构建图）
        assert client.get("/health/ready").status_code == 200

        ok = client.post("/api/chat",
                         json={"message": "理财产品A的收益率是多少", "user_id": "u1", "session_id": "d1"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["compliance_passed"] is True
        assert "3.5%" in body["response"]

        ticket = client.post("/api/chat", json={"message": "我要申请退款", "user_id": "u1", "session_id": "d2"})
        assert ticket.status_code == 200
        assert "工单号" in ticket.json()["response"]


def test_demo_mode_blocks_forbidden_claim():
    st = Settings(demo_mode=True, require_auth=False, api_keys="",
                  rate_limit_per_minute=1000, checkpoint_backend="memory")
    app = create_app(settings=st, approvals=MemoryApprovalStore())
    with TestClient(app) as client:
        resp = client.post("/api/chat",
                           json={"message": "你们能保证收益吗", "user_id": "u1", "session_id": "d3"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["compliance_passed"] is False
    assert body["approval_id"]

def test_demo_mode_with_sqlite_checkpoint(tmp_path):
    """回归：同步 SqliteSaver 在 ainvoke 下会抛 NotImplementedError（真实事故）。"""
    st = Settings(
        demo_mode=True, require_auth=False, api_keys="", rate_limit_per_minute=1000,
        checkpoint_backend="sqlite", checkpoint_path=str(tmp_path / "cp.db"),
    )
    app = create_app(settings=st, approvals=MemoryApprovalStore())
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        resp = client.post("/api/chat",
                           json={"message": "理财产品A的收益率是多少", "user_id": "u1", "session_id": "sqlite"})
        assert resp.status_code == 200, resp.text
        assert "3.5%" in resp.json()["response"]

"""AG-UI 事件编码测试（T-P2-06）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import create_app
from config import Settings
from events.agui import AGUIEncoder
from security.approvals import MemoryApprovalStore
from tests.test_streaming import StubLTM, StubSTM, TokenGraph, _Chunk, _events


def test_session_maps_to_run_started():
    enc = AGUIEncoder(thread_id="t0")
    out = enc.encode({"type": "session", "session_id": "s1"})
    assert out == [{"type": "RUN_STARTED", "threadId": "s1", "runId": enc.run_id}]


def test_stage_maps_to_step_pair():
    enc = AGUIEncoder(thread_id="s1")
    out = enc.encode({"type": "stage", "node": "knowledge_rag", "label": "检索中"})
    assert [e["type"] for e in out] == ["STEP_STARTED", "STEP_FINISHED"]
    assert out[0]["stepName"] == "knowledge_rag"
    assert out[0]["metadata"]["label"] == "检索中"


def test_tokens_open_single_message():
    enc = AGUIEncoder(thread_id="s1")
    first = enc.encode({"type": "token", "text": "年化"})
    second = enc.encode({"type": "token", "text": "收益率"})
    assert [e["type"] for e in first] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT"]
    assert [e["type"] for e in second] == ["TEXT_MESSAGE_CONTENT"]
    assert second[0]["messageId"] == first[0]["messageId"]
    assert second[0]["delta"] == "收益率"


def test_response_without_tokens_emits_full_message():
    enc = AGUIEncoder(thread_id="s1")
    out = enc.encode({"type": "response", "response": "答案", "intent": "knowledge_rag",
                      "compliance_passed": True})
    types = [e["type"] for e in out]
    assert types == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
                     "STATE_DELTA", "RUN_FINISHED"]
    assert out[1]["delta"] == "答案"


def test_response_with_approval_emits_custom_event():
    enc = AGUIEncoder(thread_id="s1")
    out = enc.encode({"type": "response", "response": "已转人工", "intent": "ticket_handler",
                      "compliance_passed": False, "approval_id": "appr-1"})
    types = [e["type"] for e in out]
    assert "CUSTOM" in types
    custom = next(e for e in out if e["type"] == "CUSTOM")
    assert custom["name"] == "approval_required"
    assert custom["value"]["approvalId"] == "appr-1"


def test_error_maps_to_run_error():
    enc = AGUIEncoder(thread_id="s1")
    out = enc.encode({"type": "error", "status": 400, "message": "注入"})
    assert out[0]["type"] == "RUN_ERROR"
    assert out[0]["code"] == 400


def test_done_is_idempotent_after_finish():
    enc = AGUIEncoder(thread_id="s1")
    enc.encode({"type": "response", "response": "x", "intent": "k", "compliance_passed": True})
    assert enc.encode({"type": "done"}) == []


def test_unknown_event_is_raw_passthrough():
    enc = AGUIEncoder(thread_id="s1")
    out = enc.encode({"type": "mystery", "value": 1})
    assert out[0]["type"] == "RAW"


# ── API 集成 ──


def _client(graph):
    st = Settings(require_auth=False, api_keys="", rate_limit_per_minute=1000)
    app = create_app(settings=st, graph=graph, short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    return TestClient(app)


def test_api_agui_protocol_sequence():
    items = [
        ("updates", {"supervisor_route": {"intent": "knowledge_rag"}}),
        ("messages", (_Chunk("年化"), {"tags": ["final_answer"], "langgraph_node": "knowledge_rag"})),
        ("messages", (_Chunk("3.5%"), {"tags": ["final_answer"], "langgraph_node": "knowledge_rag"})),
        ("updates", {"synthesize": {"final_response": "年化 3.5%", "intent": "knowledge_rag"}}),
    ]
    client = _client(TokenGraph(items))
    resp = client.post("/api/chat/stream?protocol=agui",
                       json={"message": "收益多少", "user_id": "u1", "session_id": "ag1"})
    assert resp.status_code == 200
    events = _events(resp.text)
    types = [e["type"] for e in events]

    assert types[0] == "RUN_STARTED"
    assert "STEP_STARTED" in types and "STEP_FINISHED" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert types[-1] == "RUN_FINISHED"
    deltas = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert deltas == "年化3.5%"


def test_api_legacy_protocol_unchanged():
    client = _client(TokenGraph([("updates", {"synthesize": {"final_response": "x",
                                                              "intent": "knowledge_rag"}})]))
    resp = client.post("/api/chat/stream", json={"message": "hi", "user_id": "u1", "session_id": "ag2"})
    events = _events(resp.text)
    assert events[0]["type"] == "session"
    assert {"stage", "response", "done"} & {e["type"] for e in events}


def test_api_rejects_unknown_protocol():
    client = _client(TokenGraph([]))
    resp = client.post("/api/chat/stream?protocol=websocket",
                       json={"message": "hi", "user_id": "u1"})
    assert resp.status_code == 422

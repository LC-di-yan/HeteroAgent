"""编排层测试：路由解析、聚合文案、超时重试（T-P0-02/04/10）。"""
from __future__ import annotations

import asyncio

from agents.supervisor import (
    AgentState,
    SupervisorNode,
    _is_retryable,
    _wrap_with_timeout,
    create_supervisor_graph,
    parse_route,
)
from config import Settings
from tests.conftest import FakeLLM


class _FakeMsg:
    def __init__(self, content: str):
        self.content = content


def test_parse_route_valid_tokens():
    assert parse_route("knowledge_rag") == "knowledge_rag"
    assert parse_route("ticket_handler") == "ticket_handler"
    assert parse_route("Knowledge_RAG。") == "knowledge_rag"
    assert parse_route("我选 ticket_handler，因为用户要退款") == "ticket_handler"


def test_parse_route_compliance_goes_to_knowledge():
    # 原实现把 compliance 意图直接路由到合规节点，用户得到空回复（4.1.1）
    assert parse_route("compliance_checker") == "knowledge_rag"
    assert parse_route("compliance") == "knowledge_rag"


def test_parse_route_garbage_fallback():
    assert parse_route("") == "knowledge_rag"
    assert parse_route("好的，我来处理") == "knowledge_rag"
    assert parse_route("```ticket_handler```") == "ticket_handler"


def test_route_decision_via_fake_llm():
    node = SupervisorNode(FakeLLM(["ticket_handler"]))
    state = {"messages": [_FakeMsg("我要退款")], "intent": ""}
    out = asyncio.run(node.route_decision(state))
    assert out["intent"] == "ticket_handler"


def _state(**overrides) -> AgentState:
    base = {
        "messages": [_FakeMsg("用户消息")],
        "user_id": "u1",
        "session_id": "s1",
        "trace_id": "t1",
        "intent": "knowledge_rag",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "retry_count": 0,
    }
    base.update(overrides)
    return base


def test_synthesize_no_fake_claims():
    # G10：合规失败时不得编造"工单编号已自动生成"
    node = SupervisorNode(FakeLLM())
    out = asyncio.run(node.synthesize_response(_state(compliance_passed=False)))
    text = out["final_response"]
    assert "TK-" not in text and "工单编号" not in text
    assert "人工" in text


def test_synthesize_joins_agent_outputs_only():
    node = SupervisorNode(FakeLLM())
    out = asyncio.run(node.synthesize_response(_state(
        sub_results={"knowledge_rag": "A 回复", "compliance": {"passed": True}}
    )))
    assert out["final_response"] == "A 回复"


def test_retryable_by_exception_type():
    assert _is_retryable(TimeoutError()) is True
    assert _is_retryable(ConnectionError("refused")) is True
    assert _is_retryable(ValueError("bad json")) is False


def test_timeout_wrapper_fallback():
    async def always_slow(state):
        await asyncio.sleep(5)
        return state

    settings = Settings(llm_timeout_seconds=0.01, llm_max_retries=1)
    wrapped = _wrap_with_timeout("knowledge_rag", always_slow, settings)
    out = asyncio.run(wrapped(_state()))
    assert "超时" in out["sub_results"]["knowledge_rag"]


def test_timeout_wrapper_compliance_fails_closed():
    async def boom(state):
        raise ConnectionError("down")

    settings = Settings(llm_timeout_seconds=0.01, llm_max_retries=0)
    wrapped = _wrap_with_timeout("compliance_check", boom, settings)
    out = asyncio.run(wrapped(_state()))
    assert out["compliance_passed"] is False  # fail-closed


def test_graph_builds_offline_with_stub_llm():
    """无 API key 时，显式传入 FakeLLM 可以完成构图（拓扑完整性）。"""
    graph = create_supervisor_graph(llm=FakeLLM(), settings=Settings(), enable_checkpointing=False)
    assert graph is not None
    node_names = set(graph.get_graph().nodes.keys())
    assert {"supervisor_route", "knowledge_rag", "ticket_handler", "compliance_check", "synthesize"} <= node_names

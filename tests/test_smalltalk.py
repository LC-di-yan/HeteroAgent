"""寒暄/澄清快路径测试：问候不查库、不调模型、直接给建议。"""
from __future__ import annotations

import asyncio
import re

import pytest
from langchain_core.messages import HumanMessage

from agents.compliance_checker import FORBIDDEN_TERMS
from agents.schemas import RouteDecision
from agents.smalltalk import (
    KIND_CAPABILITY,
    KIND_CLARIFY,
    KIND_GREETING,
    KIND_THANKS,
    build_reply,
    classify_smalltalk,
)
from agents.supervisor import ROUTE_SMALLTALK, SupervisorNode, parse_route, route_to_agent
from tests.fakes import StructuredFakeLLM


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好", KIND_GREETING),
        ("您好！", KIND_GREETING),
        ("hello", KIND_GREETING),
        ("在吗？", KIND_GREETING),
        ("谢谢", KIND_THANKS),
        ("谢谢！", KIND_THANKS),
        ("thanks", KIND_THANKS),
        ("你能做什么", KIND_CAPABILITY),
        ("你是谁", KIND_CAPABILITY),
        ("嗯", KIND_CLARIFY),
        ("在?", KIND_CLARIFY),
        ("", KIND_CLARIFY),
    ],
)
def test_classify_smalltalk(text: str, expected: str) -> None:
    assert classify_smalltalk(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "理财产品A的收益率是多少",
        "我要申请退款",
        "帮我查一下订单状态",
        "你们能保证收益吗",
    ],
)
def test_business_text_is_routed_normally(text: str) -> None:
    assert classify_smalltalk(text) is None


@pytest.mark.parametrize("kind", [KIND_GREETING, KIND_THANKS, KIND_CAPABILITY, KIND_CLARIFY])
def test_templates_are_clean(kind: str) -> None:
    text = build_reply(kind)
    for term in FORBIDDEN_TERMS:
        assert term not in text
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)
    assert not re.search(r"(?<!\d)\d{16,19}(?!\d)", text)


class _ExplodingLLM:
    """任何调用都失败：证明快路径没有触发模型。"""

    async def ainvoke(self, *args, **kwargs):
        raise AssertionError("smalltalk fast path must not call the LLM")

    def with_structured_output(self, *args, **kwargs):
        raise AssertionError("smalltalk fast path must not call the LLM")


def test_greeting_skips_llm_and_model_router() -> None:
    supervisor = SupervisorNode(_ExplodingLLM())
    state = {"messages": [HumanMessage(content="你好")], "sub_results": {}}
    out = asyncio.run(supervisor.route_decision(state))
    assert out["intent"] == ROUTE_SMALLTALK
    assert out["smalltalk_kind"] == KIND_GREETING


def test_unknown_route_decision_becomes_clarify() -> None:
    supervisor = SupervisorNode(StructuredFakeLLM([RouteDecision(target="unknown")]))
    state = {"messages": [HumanMessage(content="今天天气怎么样")], "sub_results": {}}
    out = asyncio.run(supervisor.route_decision(state))
    assert out["intent"] == ROUTE_SMALLTALK
    assert out["smalltalk_kind"] == KIND_CLARIFY


def test_smalltalk_reply_is_aggregated() -> None:
    supervisor = SupervisorNode(_ExplodingLLM())
    state = {
        "messages": [HumanMessage(content="你好")],
        "smalltalk_kind": KIND_GREETING,
        "sub_results": {},
    }
    out = asyncio.run(supervisor.smalltalk_response(state))
    assert out["sub_results"]["smalltalk"] == build_reply(KIND_GREETING)
    assert out["compliance_passed"] is True


def test_route_to_agent_maps_smalltalk() -> None:
    assert route_to_agent({"intent": ROUTE_SMALLTALK}) == ROUTE_SMALLTALK
    assert route_to_agent({"intent": "ticket_handler"}) == "ticket_handler"
    assert route_to_agent({"intent": "knowledge_rag"}) == "knowledge_rag"


def test_parse_route_recognizes_unknown() -> None:
    assert parse_route("unknown") == "unknown"
    assert parse_route("这属于 unknown 类别") == "unknown"
    assert parse_route("knowledge_rag") == "knowledge_rag"

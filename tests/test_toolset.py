"""MCP 工具接入 Agent 链路测试（T-P1-07）。"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents.ticket_handler import TicketHandlerAgent
from agents.toolset import build_tools, order_query, ticket_create
from tests.fakes import FakeToolCallingModel
from tracing.otel_config import install_provider, reset_tracing


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    install_provider(provider)
    try:
        yield exporter
    finally:
        reset_tracing()


def test_build_tools_exposes_four_mcp_tools():
    tools = build_tools()
    names = {t.name for t in tools}
    assert names == {"order_query", "knowledge_search", "ticket_create", "risk_check"}
    schema = next(t for t in tools if t.name == "ticket_create").args_schema
    assert "title" in schema.model_fields


def test_tool_call_emits_execute_tool_span(spans):
    raw = asyncio.run(order_query(order_id="ORD-20260401-001"))
    payload = json.loads(raw)
    assert payload["success"] is True
    span_names = [s.name for s in spans.get_finished_spans()]
    assert any(name.startswith("execute_tool order_query") for name in span_names), span_names


def test_ticket_create_tool_is_idempotent(spans):
    payload = {"title": "退款", "description": "7 天内退款", "idempotency_key": "idem-1"}
    first = asyncio.run(ticket_create(**payload))
    second = asyncio.run(ticket_create(**payload))
    id1 = json.loads(first)["result"]["ticket_id"]
    id2 = json.loads(second)["result"]["ticket_id"]
    assert id1 == id2  # 幂等键生效，不会重复建单


def test_ticket_agent_uses_tool_calls(spans):
    """模型发起 ticket_create 工具调用 → 工单真的被创建，且留下 execute_tool span。"""
    model = FakeToolCallingModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "ticket_create",
            "args": {"title": "退款申请", "description": "购买 7 天内申请退款"},
            "id": "call-1",
        }]),
        AIMessage(content="已为您创建退款工单，工单号见系统回执。"),
    ])
    agent = TicketHandlerAgent(model, tools=build_tools())
    state = {"messages": [HumanMessage(content="我要申请退款")], "user_id": "u1", "sub_results": {}}
    out = asyncio.run(agent.process(state))

    answer = out["sub_results"]["ticket_handler"]
    assert "工单" in answer

    from tooling.client import get_call_log

    calls = get_call_log(last_n=50)
    assert any(entry["tool"] == "ticket_create" for entry in calls)
    span_names = [s.name for s in spans.get_finished_spans()]
    assert any(name.startswith("execute_tool ticket_create") for name in span_names), span_names


def test_ticket_agent_falls_back_when_model_lacks_tools():
    """模型不支持 bind_tools 时不报错，退回文本解析路径。"""
    from tests.conftest import FakeLLM

    agent = TicketHandlerAgent(FakeLLM([json.dumps({
        "action": "create", "ticket_type": "general", "priority": "medium",
        "summary": "咨询", "details": "咨询",
    })]), tools=build_tools())
    assert agent._tool_agent is None  # 未构建工具 Agent
    out = asyncio.run(agent.process({
        "messages": [HumanMessage(content="我想咨询")], "user_id": "u1", "sub_results": {},
    }))
    assert "工单" in out["sub_results"]["ticket_handler"]

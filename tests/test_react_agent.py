"""create_agent 工具调用工厂测试（T-P1-02 / T-P1-07）。"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agents.react_agent import ToolCallingNotSupported, build_tool_agent, model_supports_tools
from tests.conftest import FakeLLM
from tests.fakes import FakeToolCallingModel

CALLS: list[str] = []


@tool
def ticket_lookup(ticket_id: str) -> str:
    """查询工单状态。"""
    CALLS.append(ticket_id)
    return f"{ticket_id}: resolved"


def test_model_supports_tools_detection():
    assert model_supports_tools(FakeToolCallingModel(responses=[AIMessage(content="x")])) is True
    assert model_supports_tools(FakeLLM()) is False


def test_build_tool_agent_rejects_unsupported_model():
    with pytest.raises(ToolCallingNotSupported):
        build_tool_agent(FakeLLM(), [ticket_lookup], "sys", "ticket_agent")


def test_tool_calling_loop_executes_tool():
    model = FakeToolCallingModel(responses=[
        AIMessage(content="", tool_calls=[
            {"name": "ticket_lookup", "args": {"ticket_id": "TK-1"}, "id": "call_1"}
        ]),
        AIMessage(content="工单 TK-1 状态：已解决"),
    ])
    agent = build_tool_agent(model, [ticket_lookup], "你是工单助手", "ticket_agent")
    result = agent.invoke({"messages": [HumanMessage(content="查一下 TK-1")]})

    assert CALLS == ["TK-1"]                      # 工具真的被执行
    assert result["messages"][-1].content == "工单 TK-1 状态：已解决"
    # 轨迹里包含 ToolMessage
    tool_msgs = [m for m in result["messages"] if m.__class__.__name__ == "ToolMessage"]
    assert tool_msgs and "resolved" in tool_msgs[0].content

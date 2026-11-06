"""工单 Agent：解析失败不得产生副作用。"""
from __future__ import annotations

import asyncio

from agents.ticket_handler import TicketHandlerAgent, TicketStore
from tests.conftest import FakeLLM


class _Msg:
    def __init__(self, content):
        self.content = content


def _state(text: str) -> dict:
    return {"messages": [_Msg(text)], "user_id": "u1", "sub_results": {}}


def test_parse_failure_creates_no_ticket():
    store = TicketStore()
    agent = TicketHandlerAgent(FakeLLM(["抱歉，我无法生成 JSON"]), ticket_store=store)
    out = asyncio.run(agent.process(_state("我要退款")))
    assert store._tickets == {}  # 无副作用
    assert "没能完整理解" in out["sub_results"]["ticket_handler"]


def test_valid_json_creates_ticket():
    payload = '{"action": "create", "ticket_type": "refund", "priority": "high", "summary": "退款", "details": "7天内退款"}'
    store = TicketStore()
    agent = TicketHandlerAgent(FakeLLM([payload]), ticket_store=store)
    out = asyncio.run(agent.process(_state("我要退款")))
    assert len(store._tickets) == 1
    assert "工单号" in out["sub_results"]["ticket_handler"]


def test_ticket_id_query_bypasses_llm():
    llm = FakeLLM(["SHOULD_NOT_BE_USED"])
    agent = TicketHandlerAgent(llm, ticket_store=TicketStore())
    out = asyncio.run(agent.process(_state("查一下 TK-20261106-ABC123 的进度")))
    assert llm.calls == []  # 正则预判命中，不调用 LLM
    assert "未找到工单号" in out["sub_results"]["ticket_handler"]

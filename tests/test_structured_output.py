"""结构化输出契约测试（T-P1-03）。"""
from __future__ import annotations

import asyncio

from agents.compliance_checker import ComplianceCheckerAgent
from agents.schemas import ComplianceVerdict, RouteDecision, TicketAction
from agents.supervisor import SupervisorNode
from agents.ticket_handler import TicketHandlerAgent, TicketStore
from tests.fakes import StructuredFakeLLM


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


def _state(**kw) -> dict:
    base = {"messages": [_Msg("我要退款")], "user_id": "u1", "session_id": "s1",
            "trace_id": "t1", "intent": "", "sub_results": {}, "compliance_passed": True}
    base.update(kw)
    return base


def test_schema_defaults_are_safe():
    # 默认值必须是"最小副作用"：合规默认不通过、工单默认 general/medium
    verdict = ComplianceVerdict()
    assert verdict.passed is False
    assert verdict.risk_level == "medium"
    assert TicketAction().action == "create"
    assert TicketAction().ticket_type == "general"
    assert RouteDecision().target == "knowledge_rag"


def test_route_uses_structured_output():
    llm = StructuredFakeLLM([RouteDecision(target="ticket_handler", confidence=0.92)])
    node = SupervisorNode(llm)
    out = asyncio.run(node.route_decision(_state()))
    assert out["intent"] == "ticket_handler"
    assert llm.calls, "结构化调用应被触发"


def test_route_compliance_target_maps_to_knowledge():
    llm = StructuredFakeLLM([RouteDecision(target="compliance_checker")])
    out = asyncio.run(SupervisorNode(llm).route_decision(_state()))
    assert out["intent"] == "knowledge_rag"


def test_compliance_structured_verdict_applied():
    llm = StructuredFakeLLM([ComplianceVerdict(passed=False, risk_level="high",
                                               violations=["越权承诺"], suggestions=["改"])])
    result = asyncio.run(ComplianceCheckerAgent(llm).llm_check("我们保证收益"))
    assert result.passed is False
    assert result.risk_level == "high"
    assert "越权承诺" in result.violations


def test_ticket_structured_action_creates_ticket():
    store = TicketStore()
    llm = StructuredFakeLLM([TicketAction(action="create", ticket_type="refund",
                                          priority="high", summary="退款", details="7天")])
    agent = TicketHandlerAgent(llm, ticket_store=store)
    out = asyncio.run(agent.process(_state()))
    assert len(store._tickets) == 1
    assert "工单号" in out["sub_results"]["ticket_handler"]


def test_structured_validation_error_falls_back_not_side_effect():
    """schema 非法（优先级写错）→ 结构化失败 → 文本兜底；兜底失败则无副作用。"""
    class _Bad:
        def with_structured_output(self, schema, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            return {"action": "create", "priority": "超级高"}  # 非法枚举

    store = TicketStore()
    agent = TicketHandlerAgent(_Bad(), ticket_store=store)
    out = asyncio.run(agent.process(_state()))
    assert store._tickets == {}  # 不建单
    assert "没能完整理解" in out["sub_results"]["ticket_handler"]

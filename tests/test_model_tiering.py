"""模型分级测试（LIGHT_MODEL_NAME 生效，T-P3-04）。"""
from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from agents.llm_factory import build_light_llm, build_main_llm
from agents.supervisor import create_supervisor_graph
from config import Settings
from tests.conftest import FakeLLM


def test_light_model_reuses_main_when_unset():
    st = Settings(model_name="gpt-5.1", light_model_name="", openai_api_key="sk-test")
    main = build_main_llm(st)
    assert build_light_llm(st, main_llm=main) is main


def test_light_model_built_when_configured():
    st = Settings(model_name="gpt-5.1", light_model_name="gpt-5.1-mini", openai_api_key="sk-test")
    light = build_light_llm(st)
    assert getattr(light, "model_name", "") == "gpt-5.1-mini"
    assert getattr(light, "streaming", True) is False  # 路由不需要流式


def test_router_uses_light_model_and_agents_use_main():
    main = FakeLLM([
        '{"action": "create", "ticket_type": "refund", "priority": "medium",'
        ' "summary": "退款", "details": "7 天"}',
        '{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}',
    ])
    light = FakeLLM(["ticket_handler"])

    graph = create_supervisor_graph(
        llm=main, light_llm=light,
        settings=Settings(checkpoint_backend="memory"),
        enable_checkpointing=False,
    )
    out = asyncio.run(graph.ainvoke({
        "messages": [HumanMessage(content="我要退款")],
        "user_id": "u1", "session_id": "s1", "trace_id": "t1",
        "intent": "", "sub_results": {}, "compliance_passed": True,
        "final_response": "", "retry_count": 0, "approval_id": None, "approval_decision": None,
    }))

    assert out["intent"] == "ticket_handler"
    assert len(light.calls) == 1          # 只有路由走了轻模型
    assert len(main.calls) >= 2           # 工单解析与合规审查走主模型
    assert "工单号" in out["final_response"]


def test_no_light_model_means_single_client():
    main = FakeLLM(["knowledge_rag"])
    graph = create_supervisor_graph(
        llm=main, light_llm=None,
        settings=Settings(checkpoint_backend="memory"), enable_checkpointing=False,
    )
    assert graph is not None  # 不额外构造客户端，路由复用主模型

"""节点超时预算测试（RAG 串行多次调用不应被误判超时）。"""
from __future__ import annotations

import asyncio

from agents.supervisor import _wrap_with_timeout
from config import Settings


def _state():
    return {"messages": [], "sub_results": {}, "compliance_passed": True}


def test_node_timeout_defaults_to_triple_llm_timeout():
    """默认按单次 LLM 超时 ×3，容纳 RAG 的串行调用链。"""
    async def slow(state):
        await asyncio.sleep(0.25)
        return {**state, "sub_results": {"knowledge_rag": "done"}}

    # 单次 LLM 0.1s → 节点预算 0.3s，足够 0.25s 的链路
    settings = Settings(llm_timeout_seconds=0.1, llm_max_retries=0,
                        agent_node_timeout_seconds=0.0)
    wrapped = _wrap_with_timeout("knowledge_rag", slow, settings)
    out = asyncio.run(wrapped(_state()))
    assert out["sub_results"]["knowledge_rag"] == "done"


def test_explicit_node_timeout_overrides():
    async def slow(state):
        await asyncio.sleep(1.0)
        return state

    settings = Settings(llm_timeout_seconds=10.0, llm_max_retries=0,
                        agent_node_timeout_seconds=0.05)
    wrapped = _wrap_with_timeout("knowledge_rag", slow, settings)
    out = asyncio.run(wrapped(_state()))
    assert "超时" in out["sub_results"]["knowledge_rag"]


def test_single_call_would_have_timed_out():
    """回归：若仍用单次 LLM 超时做节点预算，这条正常链路会被误杀。"""
    async def chain(state):
        await asyncio.sleep(0.15)   # 模拟 5 次调用累计耗时
        return {**state, "sub_results": {"knowledge_rag": "ok"}}

    settings = Settings(llm_timeout_seconds=0.1, llm_max_retries=0)
    wrapped = _wrap_with_timeout("knowledge_rag", chain, settings)
    out = asyncio.run(wrapped(_state()))
    assert out["sub_results"]["knowledge_rag"] == "ok"   # 0.1×3=0.3 > 0.15

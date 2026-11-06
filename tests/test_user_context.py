"""跨会话用户事实的消费侧测试（T-P2-02）。"""
from __future__ import annotations

import asyncio
import json

from agents.knowledge_rag import KnowledgeRAGAgent
from agents.ticket_handler import TicketHandlerAgent, TicketStore
from config import Settings
from memory.context_store import ContextStore
from tests.conftest import FakeLLM


class _Msg:
    def __init__(self, content: str):
        self.content = content


class StubMemory:
    def __init__(self, docs):
        self.docs = docs
        self._documents = []

    def search(self, query, top_k=5):
        return self.docs[:top_k]

    async def asearch(self, query, top_k=5):
        return self.docs[:top_k]


def _state(text: str, user_id: str = "u1") -> dict:
    return {"messages": [_Msg(text)], "user_id": user_id, "session_id": "s1",
            "trace_id": "t1", "sub_results": {}, "compliance_passed": True}


def test_ticket_agent_remembers_ticket_id():
    store = ContextStore()
    payload = json.dumps({"action": "create", "ticket_type": "refund", "priority": "high",
                          "summary": "退款", "details": "7 天"})
    agent = TicketHandlerAgent(FakeLLM([payload]), ticket_store=TicketStore(), context_store=store)
    out = asyncio.run(agent.process(_state("我要退款")))
    assert "工单号" in out["sub_results"]["ticket_handler"]

    facts = store.get_facts("u1")
    assert len(facts) == 1
    assert facts[0].kind == "ticket"
    assert facts[0].value.startswith("TK-")


def test_knowledge_agent_injects_user_context():
    store = ContextStore()
    store.put_fact("u1", "ticket", "TK-20261106-ABC123")

    llm = FakeLLM(["理财收益", "已为您查询到历史工单", "PASS"])
    memory = StubMemory([{"id": "d1", "content": "理财产品A收益 3.5%",
                          "source": "product_faq.md", "score": 0.9}])
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3),
                              context_store=store)

    asyncio.run(agent.process(_state("我的退款进度")))
    joined = " ".join(
        getattr(m, "content", "") for call in llm.calls for m in (call if isinstance(call, list) else [call])
    )
    assert "该用户近期记录" in joined
    assert "TK-20261106-ABC123" in joined


def test_knowledge_agent_without_facts_has_no_context_block():
    llm = FakeLLM(["理财收益", "答案", "PASS"])
    memory = StubMemory([{"id": "d1", "content": "理财产品A收益 3.5%",
                          "source": "product_faq.md", "score": 0.9}])
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3),
                              context_store=ContextStore())
    asyncio.run(agent.process(_state("收益多少")))
    joined = " ".join(
        getattr(m, "content", "") for call in llm.calls for m in (call if isinstance(call, list) else [call])
    )
    assert "该用户近期记录" not in joined


def test_context_store_failure_does_not_break_answer():
    class BrokenStore:
        def format_for_prompt(self, user_id):
            raise RuntimeError("store down")

    llm = FakeLLM(["理财收益", "答案", "PASS"])
    memory = StubMemory([{"id": "d1", "content": "理财产品A收益 3.5%",
                          "source": "product_faq.md", "score": 0.9}])
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3),
                              context_store=BrokenStore())
    out = asyncio.run(agent.process(_state("收益多少")))
    assert out["sub_results"]["knowledge_rag"] == "答案"

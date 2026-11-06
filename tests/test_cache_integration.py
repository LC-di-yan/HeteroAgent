"""语义缓存在 RAG 链路中的行为测试（T-P3-04）。"""
from __future__ import annotations

import asyncio

from agents.knowledge_rag import KnowledgeRAGAgent
from config import Settings
from memory.context_store import ContextStore
from memory.semantic_cache import SemanticCache
from tests.conftest import FakeLLM


class _Msg:
    def __init__(self, content: str):
        self.content = content


class StubMemory:
    def __init__(self, docs):
        self.docs = docs
        self._documents = docs

    def search(self, query, top_k=5):
        return self.docs[:top_k]

    async def asearch(self, query, top_k=5):
        return self.docs[:top_k]


DOCS = [{"id": "d1", "content": "理财产品A收益 3.5%-5.2%", "source": "product_faq.md", "score": 0.9}]


def _state(text: str, user_id: str = "u1") -> dict:
    return {"messages": [_Msg(text)], "user_id": user_id, "tenant_id": "t1",
            "session_id": "s1", "trace_id": "tr1", "sub_results": {}, "compliance_passed": True}


def _agent(llm, cache, context_store=None):
    return KnowledgeRAGAgent(
        llm, long_term_memory=StubMemory(DOCS),
        settings=Settings(rag_confidence_threshold=0.3, semantic_cache_enabled=True),
        context_store=context_store, semantic_cache=cache,
    )


def test_second_identical_question_hits_cache():
    llm = FakeLLM(["理财收益", "理财产品A收益 3.5%", "PASS"])
    cache = SemanticCache()
    agent = _agent(llm, cache)

    first = asyncio.run(agent.process(_state("理财产品A收益多少")))
    calls_after_first = len(llm.calls)
    second = asyncio.run(agent.process(_state("理财产品A收益多少")))

    assert first["sub_results"]["knowledge_rag"] == second["sub_results"]["knowledge_rag"]
    assert second["sub_results"]["cache"]["hit"] is True
    assert len(llm.calls) == calls_after_first  # 第二次没有调用模型


def test_punctuation_variant_hits_cache():
    llm = FakeLLM(["理财收益", "答案", "PASS"])
    cache = SemanticCache()
    agent = _agent(llm, cache)
    asyncio.run(agent.process(_state("理财产品A收益多少")))
    hit = asyncio.run(agent.process(_state("理财产品A收益多少？")))
    assert hit["sub_results"]["cache"]["hit"] is True


def test_personalised_requests_bypass_cache():
    store = ContextStore()
    store.put_fact("u1", "ticket", "TK-20261106-ABC123")
    llm = FakeLLM(["理财收益", "答案1", "PASS", "理财收益", "答案2", "PASS"])
    cache = SemanticCache()
    agent = _agent(llm, cache, context_store=store)

    first = asyncio.run(agent.process(_state("我的退款进度")))
    second = asyncio.run(agent.process(_state("我的退款进度")))

    assert first["sub_results"].get("cache") is None
    assert second["sub_results"].get("cache") is None
    assert len(llm.calls) >= 6  # 两次都走了完整链路
    assert cache.stats()["entries"] == 0  # 个性化回答不写缓存


def test_disabled_cache_does_not_alter_flow():
    llm = FakeLLM(["理财收益", "答案", "PASS", "理财收益", "答案", "PASS"])
    agent = _agent(llm, SemanticCache(enabled=False))
    asyncio.run(agent.process(_state("收益多少")))
    out = asyncio.run(agent.process(_state("收益多少")))
    assert out["sub_results"].get("cache") is None

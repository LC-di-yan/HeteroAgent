"""知识检索走 MCP 工具的测试（T-P1-07 收尾）。"""
from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents.knowledge_rag import KnowledgeRAGAgent
from config import Settings
from tests.conftest import FakeLLM
from tooling.server import has_knowledge_backend, reset_knowledge_backend, set_knowledge_backend
from tracing.otel_config import install_provider, reset_tracing


class _Msg:
    def __init__(self, content: str):
        self.content = content


class StubMemory:
    def __init__(self, docs):
        self.docs = docs
        self._documents = docs
        self.calls = 0

    def search(self, query, top_k=5):
        self.calls += 1
        return self.docs[:top_k]

    async def asearch(self, query, top_k=5):
        self.calls += 1
        return self.docs[:top_k]


DOCS = [{"id": "d1", "content": "理财产品A收益 3.5%-5.2%", "source": "product_faq.md", "score": 0.9}]


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
        reset_knowledge_backend()


def _state(text: str = "理财产品A收益多少") -> dict:
    return {"messages": [_Msg(text)], "user_id": "u1", "tenant_id": "t1",
            "session_id": "s1", "trace_id": "tr1", "sub_results": {}, "compliance_passed": True}


def test_backend_injection_is_used_by_tool():
    async def backend(query: str, top_k: int):
        return [{"content": f"真实库命中: {query}", "source": "kb.md", "score": 0.88}]

    set_knowledge_backend(backend)
    assert has_knowledge_backend() is True

    from tooling.client import call_tool

    result = asyncio.run(call_tool("knowledge_search", {"query": "退款", "top_k": 2}))
    assert result["success"] is True
    payload = result["result"]
    assert payload["backend"] == "injected"
    assert payload["results"][0]["content"] == "真实库命中: 退款"


def test_rag_uses_tool_when_backend_injected(spans):
    async def backend(query: str, top_k: int):
        return [{"content": "工具返回的知识：年化 3.5%", "source": "kb.md", "score": 0.92}]

    set_knowledge_backend(backend)
    llm = FakeLLM(["理财收益", "工具返回的知识：年化 3.5%", "PASS"])
    memory = StubMemory(DOCS)
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3))

    out = asyncio.run(agent.process(_state()))
    assert "3.5%" in out["sub_results"]["knowledge_rag"]
    assert memory.calls == 0  # 没有走直连检索

    names = [s.name for s in spans.get_finished_spans()]
    assert any(n.startswith("execute_tool knowledge_search") for n in names), names


def test_falls_back_to_direct_retrieval_when_backend_fails(spans):
    async def broken(query: str, top_k: int):
        raise ConnectionError("backend down")

    set_knowledge_backend(broken)
    llm = FakeLLM(["理财收益", "直连检索的答案", "PASS"])
    memory = StubMemory(DOCS)
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3))

    out = asyncio.run(agent.process(_state()))
    assert out["sub_results"]["knowledge_rag"] == "直连检索的答案"
    assert memory.calls >= 1  # 回退到直连检索


def test_direct_path_when_no_backend():
    reset_knowledge_backend()
    llm = FakeLLM(["理财收益", "答案", "PASS"])
    memory = StubMemory(DOCS)
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3))
    asyncio.run(agent.process(_state()))
    assert memory.calls >= 1


def test_tool_can_be_disabled_by_settings():
    async def backend(query: str, top_k: int):
        return [{"content": "工具结果", "source": "kb.md", "score": 0.9}]

    set_knowledge_backend(backend)
    llm = FakeLLM(["理财收益", "答案", "PASS"])
    memory = StubMemory(DOCS)
    agent = KnowledgeRAGAgent(llm, long_term_memory=memory,
                              settings=Settings(rag_confidence_threshold=0.3,
                                                rag_via_mcp_tool=False))
    asyncio.run(agent.process(_state()))
    assert memory.calls >= 1  # 显式关闭时走直连

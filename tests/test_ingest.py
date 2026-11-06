"""Contextual Retrieval 入库测试（T-P2-01）。"""
from __future__ import annotations

import asyncio

import numpy as np

from memory.ingest import ingest_documents, ingest_text, make_context
from memory.long_term import LongTermMemory


class FakeLLM:
    def __init__(self, reply: str = "本节属于退款政策，说明 7 天内退款的办理方式。", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list = []

    async def ainvoke(self, prompt):  # noqa: ANN001
        self.calls.append(prompt)
        if self.fail:
            raise ConnectionError("llm down")

        class _M:
            content = self.reply

        return _M()


def _embed(text: str) -> list[float]:
    """确定性测试向量：按字符散列，保证同文本同向量。"""
    vec = np.zeros(8, dtype=np.float32)
    for i, ch in enumerate(text):
        vec[i % 8] += (ord(ch) % 17) / 17.0
    norm = np.linalg.norm(vec)
    return (vec / (norm or 1.0)).tolist()


def _memory(tmp_path) -> LongTermMemory:
    return LongTermMemory(index_path=str(tmp_path / "idx"), embedding_dim=8, embedding_fn=_embed)


def test_make_context_returns_llm_text():
    ctx = asyncio.run(make_context(FakeLLM(), "整篇文档", "片段", source="refund.md"))
    assert "退款政策" in ctx


def test_make_context_failure_is_soft():
    ctx = asyncio.run(make_context(FakeLLM(fail=True), "文档", "片段"))
    assert ctx == ""


def test_ingest_attaches_context_but_keeps_content_clean(tmp_path):
    mem = _memory(tmp_path)
    text = "退款政策：购买后 7 天内可申请无理由退款。\\n\\n超过 7 天需提供合理原因，3-5 个工作日原路退回。"
    count = asyncio.run(ingest_text(mem, FakeLLM(), text, source="refund.md"))
    assert count >= 1

    docs = mem._documents
    assert docs, "应写入至少一条"
    for doc in docs:
        assert "退款政策" in doc["content"] or "合理原因" in doc["content"]
        assert "本节属于退款政策" not in doc["content"]  # 上下文不污染正文
        assert "本节属于退款政策" in (doc["metadata"].get("context") or "")


def test_context_is_searchable_by_bm25(tmp_path):
    mem = _memory(tmp_path)
    # 正文里没有"营业厅"，只有上下文提到 → 用于验证上下文进入了索引
    asyncio.run(ingest_text(mem, FakeLLM(reply="开户指南，需到营业厅办理。"),
                            "开户需要身份证与风险评估问卷。", source="account.md"))
    hits = mem._bm25_search("营业厅", top_k=3)
    assert hits, "上下文中的词应能命中"


def test_ingest_documents_batch(tmp_path):
    mem = _memory(tmp_path)
    total = asyncio.run(ingest_documents(mem, FakeLLM(), [
        {"content": "文档一：理财产品收益说明。", "source": "a.md"},
        {"content": "文档二：退款说明。", "source": "b.md"},
    ]))
    assert total >= 2
    assert len(mem._documents) >= 2

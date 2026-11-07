"""API 重排器测试（离线，用桩替换 HTTP）。"""
from __future__ import annotations

import asyncio

import memory.rerank_api as ra
from config import Settings
from memory.rerank_api import ApiReranker, build_api_reranker


def test_build_requires_endpoint_and_key():
    assert build_api_reranker(Settings(rerank_api_base_url="", rerank_api_key="")) is None
    assert build_api_reranker(Settings(rerank_api_base_url="http://x", rerank_api_key="")) is None
    rr = build_api_reranker(Settings(rerank_api_base_url="http://x", rerank_api_key="k"))
    assert isinstance(rr, ApiReranker)


def test_rerank_orders_by_relevance(monkeypatch):
    def fake_post(base, key, model, query, documents, top_n, timeout):
        return {"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
        ]}

    monkeypatch.setattr(ra, "_post_rerank", fake_post)
    rr = ApiReranker(base_url="http://x", api_key="k")
    docs = [{"content": "开户"}, {"content": "退款"}]
    out = asyncio.run(rr.rerank("我想退款", docs, top_k=2))
    assert out is not None
    assert out[0]["content"] == "退款"
    assert out[0]["rerank_score"] == 0.9


def test_rerank_failure_returns_none(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(ra, "_post_rerank", boom)
    rr = ApiReranker(base_url="http://x", api_key="k")
    assert asyncio.run(rr.rerank("q", [{"content": "a"}])) is None


def test_rerank_ignores_out_of_range_index(monkeypatch):
    def fake_post(*args, **kwargs):
        return {"results": [{"index": 99, "relevance_score": 1.0},
                            {"index": 0, "relevance_score": 0.5}]}

    monkeypatch.setattr(ra, "_post_rerank", fake_post)
    rr = ApiReranker(base_url="http://x", api_key="k")
    out = asyncio.run(rr.rerank("q", [{"content": "a"}], top_k=2))
    assert out is not None and len(out) == 1


def test_empty_documents_short_circuits():
    rr = ApiReranker(base_url="http://x", api_key="k")
    assert asyncio.run(rr.rerank("q", [])) == []

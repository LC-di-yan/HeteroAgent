"""语义缓存测试（T-P3-04）。"""
from __future__ import annotations

import asyncio

from memory.semantic_cache import SemanticCache, normalize_query


def _embed_factory(table: dict[str, list[float]]):
    async def _embed(text: str) -> list[float] | None:
        return table.get(normalize_query(text))

    return _embed


def test_normalize_query():
    assert normalize_query("  理财产品A的收益率是多少？ ") == "理财产品a的收益率是多少"
    assert normalize_query("Hello, World!") == "helloworld"


def test_exact_hit_after_store():
    cache = SemanticCache()
    asyncio.run(cache.store("退款政策是什么", "7 天内可退", tenant="t1"))
    hit = asyncio.run(cache.lookup("退款政策是什么？", tenant="t1"))  # 标点差异也应命中
    assert hit is not None and hit.match == "exact"
    assert hit.answer == "7 天内可退"


def test_tenant_isolation():
    cache = SemanticCache()
    asyncio.run(cache.store("退款政策", "答案A", tenant="t1"))
    assert asyncio.run(cache.lookup("退款政策", tenant="t2")) is None


def test_kb_version_invalidates():
    cache = SemanticCache()
    asyncio.run(cache.store("退款政策", "旧答案", tenant="t1", kb_version="v1"))
    assert asyncio.run(cache.lookup("退款政策", tenant="t1", kb_version="v2")) is None


def test_semantic_hit_with_embeddings():
    cache = SemanticCache(similarity_threshold=0.95)
    table = {
        "怎么退钱": [1.0, 0.0, 0.0],
        "退款怎么操作": [0.99, 0.05, 0.0],   # 近义问法
        "开户流程": [0.0, 1.0, 0.0],
    }
    embed = _embed_factory(table)
    asyncio.run(cache.store("怎么退钱", "7 天内可退", tenant="t1", embed=embed))

    hit = asyncio.run(cache.lookup("退款怎么操作", tenant="t1", embed=embed))
    assert hit is not None and hit.match == "semantic"
    assert hit.similarity >= 0.95
    assert hit.answer == "7 天内可退"


def test_below_threshold_misses():
    cache = SemanticCache(similarity_threshold=0.99)
    table = {"怎么退钱": [1.0, 0.0], "开户": [0.5, 0.5]}
    embed = _embed_factory(table)
    asyncio.run(cache.store("怎么退钱", "答案", tenant="t1", embed=embed))
    assert asyncio.run(cache.lookup("开户", tenant="t1", embed=embed)) is None


def test_ttl_expiry(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("memory.semantic_cache.time.time", lambda: clock["now"])
    cache = SemanticCache(ttl_seconds=10)
    asyncio.run(cache.store("问题", "答案"))
    assert asyncio.run(cache.lookup("问题")) is not None
    clock["now"] += 11
    assert asyncio.run(cache.lookup("问题")) is None


def test_lru_eviction():
    cache = SemanticCache(max_entries=2)
    for i in range(3):
        asyncio.run(cache.store(f"问题{i}", f"答案{i}"))
    assert cache.stats()["entries"] == 2
    assert asyncio.run(cache.lookup("问题0")) is None
    assert asyncio.run(cache.lookup("问题2")) is not None


def test_disabled_cache_never_hits():
    cache = SemanticCache(enabled=False)
    asyncio.run(cache.store("问题", "答案"))
    assert asyncio.run(cache.lookup("问题")) is None
    assert cache.stats()["entries"] == 0


def test_stats_counts_hits():
    cache = SemanticCache()
    asyncio.run(cache.store("q", "a"))
    asyncio.run(cache.lookup("q"))
    asyncio.run(cache.lookup("q"))
    assert cache.stats()["hits"] == 2

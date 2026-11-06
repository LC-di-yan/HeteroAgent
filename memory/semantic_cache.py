"""语义缓存（T-P3-04）。

目标：命中"同一个问题（或近义问法）"时直接返回缓存答案，省一次完整 RAG 链路。

安全边界（重要）：
- **只缓存非个性化回答**：带用户事实（工单号等）的请求不缓存、也不读缓存；
- 按租户隔离：不同 tenant 的缓存互不可见；
- 版本化：知识库变更（kb_version）后旧缓存自然失效；
- 可精确可语义：没有 embedding 能力时退化为"归一化精确匹配"，不生成伪向量。

没有 embedding 时不假装语义能力——这是 G7 的同一条原则。
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float] | None]]


def normalize_query(query: str) -> str:
    """归一化：小写、去标点与空白、压缩同义空白。用于精确命中。"""
    text = (query or "").strip().lower()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】]", "", text)
    return text


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class CacheEntry:
    answer: str
    created_at: float
    key: str
    vector: list[float] | None = None
    hits: int = 0
    sources: list[str] = field(default_factory=list)


@dataclass
class CacheHit:
    answer: str
    match: str  # exact / semantic
    similarity: float
    age_seconds: float


class SemanticCache:
    """进程内语义缓存（多副本时可整体替换为 Redis 实现）。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 1000,
        similarity_threshold: float = 0.95,
        enabled: bool = True,
    ) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.threshold = similarity_threshold
        self.enabled = enabled
        self._data: dict[tuple[str, str], CacheEntry] = {}

    # ── 内部 ──

    def _scoped_key(self, tenant: str, kb_version: str, query: str) -> tuple[str, str]:
        digest = hashlib.md5(normalize_query(query).encode("utf-8")).hexdigest()
        return (f"{tenant}|{kb_version}", digest)

    def _purge_expired(self, now: float) -> None:
        expired = [k for k, v in self._data.items() if now - v.created_at > self.ttl]
        for key in expired:
            self._data.pop(key, None)

    def _evict_if_needed(self) -> None:
        if len(self._data) <= self.max_entries:
            return
        # 按最后创建时间淘汰最旧的（近似 LRU，无需完整访问序）
        ordered = sorted(self._data.items(), key=lambda kv: kv[1].created_at)
        for key, _ in ordered[: len(self._data) - self.max_entries]:
            self._data.pop(key, None)

    # ── 公共接口 ──

    async def lookup(
        self,
        query: str,
        *,
        tenant: str = "default",
        kb_version: str = "",
        embed: EmbedFn | None = None,
    ) -> CacheHit | None:
        if not self.enabled or not query:
            return None
        now = time.time()
        self._purge_expired(now)

        key = self._scoped_key(tenant, kb_version, query)
        entry = self._data.get(key)
        if entry is not None:
            entry.hits += 1
            return CacheHit(answer=entry.answer, match="exact", similarity=1.0,
                            age_seconds=now - entry.created_at)

        if embed is None:
            return None
        vector = await embed(query)
        if not vector:
            return None

        scope = f"{tenant}|{kb_version}"
        best: tuple[float, CacheEntry] | None = None
        for (cached_scope, _), candidate in self._data.items():
            if cached_scope != scope or not candidate.vector:
                continue
            similarity = _cosine(vector, candidate.vector)
            if best is None or similarity > best[0]:
                best = (similarity, candidate)
        if best is not None and best[0] >= self.threshold:
            best[1].hits += 1
            return CacheHit(answer=best[1].answer, match="semantic", similarity=best[0],
                            age_seconds=now - best[1].created_at)
        return None

    async def store(
        self,
        query: str,
        answer: str,
        *,
        tenant: str = "default",
        kb_version: str = "",
        sources: list[str] | None = None,
        embed: EmbedFn | None = None,
    ) -> None:
        if not self.enabled or not query or not answer:
            return
        vector = await embed(query) if embed is not None else None
        key = self._scoped_key(tenant, kb_version, query)
        self._data[key] = CacheEntry(
            answer=answer, created_at=time.time(), key=key, vector=vector,
            sources=list(sources or []),
        )
        self._evict_if_needed()

    def stats(self) -> dict[str, Any]:
        now = time.time()
        self._purge_expired(now)
        return {
            "entries": len(self._data),
            "hits": sum(entry.hits for entry in self._data.values()),
            "ttl_seconds": self.ttl,
            "similarity_threshold": self.threshold,
            "enabled": self.enabled,
        }

    def clear(self) -> None:
        self._data.clear()


__all__ = ["SemanticCache", "CacheHit", "CacheEntry", "normalize_query"]

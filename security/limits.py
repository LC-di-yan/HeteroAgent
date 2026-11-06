"""限流：内存（单副本）与 Redis（多副本共享）（T-P0-07 / T-P2-07）。

两者暴露同一异步接口：

    allowed, retry_after = await limiter.allow(key)

Redis 实现用 ZSET 做滑动窗口，多副本共享同一配额；Redis 不可用时
按可用性优先降级为进程内限流并打告警（限流失效不阻断业务）。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class RateLimiter:
    """进程内滑动窗口限流（单副本够用）。"""

    def __init__(self, per_minute: int = 60, max_keys: int = 10_000, window_seconds: float = 60.0):
        self.per_minute = max(1, per_minute)
        self.window = window_seconds
        self.max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}

    def _allow_sync(self, key: str, now: float | None = None) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_keys:
                self._evict_oldest(now)
            bucket = deque()
            self._buckets[key] = bucket
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            retry_after = max(0.0, bucket[0] + self.window - now)
            return False, retry_after
        bucket.append(now)
        return True, 0.0

    async def allow(self, key: str) -> tuple[bool, float]:
        return self._allow_sync(key)

    def _evict_oldest(self, now: float) -> None:
        cutoff = now - self.window
        stale = [k for k, b in self._buckets.items() if not b or b[-1] <= cutoff]
        for k in stale:
            self._buckets.pop(k, None)
        if len(self._buckets) >= self.max_keys:  # 仍然满 → 丢最早插入的一批
            for k in list(self._buckets)[: self.max_keys // 10 + 1]:
                self._buckets.pop(k, None)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)


class RedisRateLimiter:
    """Redis 滑动窗口限流：多副本共享配额。"""

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        per_minute: int = 60,
        window_seconds: float = 60.0,
        *,
        client: Any = None,
        prefix: str = "heteroagent:rl",
    ) -> None:
        self.per_minute = max(1, per_minute)
        self.window = window_seconds
        self._url = url
        self._client = client
        self._prefix = prefix
        self._fallback = RateLimiter(per_minute=per_minute, window_seconds=window_seconds)
        self._degraded = False

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def allow(self, key: str) -> tuple[bool, float]:
        now = time.time()
        zkey = f"{self._prefix}:{key}"
        try:
            client = await self._redis()
            member = f"{now}:{uuid.uuid4().hex[:8]}"
            pipe = client.pipeline()
            pipe.zremrangebyscore(zkey, 0, now - self.window)
            pipe.zcard(zkey)
            pipe.zadd(zkey, {member: now})
            pipe.expire(zkey, int(self.window) + 1)
            _, count, _, _ = await pipe.execute()
        except Exception as exc:  # Redis 故障 → 降级为进程内限流
            if not self._degraded:
                logger.warning("Redis 限流不可用，降级为进程内限流: %s", type(exc).__name__)
                self._degraded = True
            return await self._fallback.allow(key)

        if count >= self.per_minute:
            try:
                await client.zrem(zkey, member)  # 超限不计数
                oldest = await client.zrange(zkey, 0, 0, withscores=True)
                retry_after = (oldest[0][1] + self.window - now) if oldest else self.window
            except Exception:  # pragma: no cover
                retry_after = self.window
            return False, max(0.0, retry_after)
        return True, 0.0


def build_rate_limiter(settings: Any, *, client: Any = None):
    """按配置构建限流器：memory（默认）或 redis（多副本）。"""
    backend = getattr(settings, "rate_limit_backend", "memory")
    per_minute = int(getattr(settings, "rate_limit_per_minute", 60))
    if backend == "redis":
        logger.info("限流后端: Redis（多副本共享）")
        return RedisRateLimiter(
            getattr(settings, "redis_url", "redis://localhost:6379/0"),
            per_minute=per_minute,
            client=client,
        )
    return RateLimiter(per_minute=per_minute)

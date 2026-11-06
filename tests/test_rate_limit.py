"""限流实现测试（内存 / Redis 滑动窗口，T-P2-07）。"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis

from config import Settings
from security.limits import RateLimiter, RedisRateLimiter, build_rate_limiter


def test_memory_limiter_blocks_after_quota():
    limiter = RateLimiter(per_minute=3)
    results = [asyncio.run(limiter.allow("k")) for _ in range(4)]
    assert [r[0] for r in results] == [True, True, True, False]
    assert results[-1][1] > 0


def test_memory_limiter_isolates_keys():
    limiter = RateLimiter(per_minute=1)
    assert asyncio.run(limiter.allow("a"))[0] is True
    assert asyncio.run(limiter.allow("b"))[0] is True
    assert asyncio.run(limiter.allow("a"))[0] is False


def test_build_rate_limiter_dispatch():
    assert isinstance(build_rate_limiter(Settings(rate_limit_backend="memory")), RateLimiter)
    redis_limiter = build_rate_limiter(Settings(rate_limit_backend="redis"))
    assert isinstance(redis_limiter, RedisRateLimiter)


def _fake_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def test_redis_limiter_quota_and_retry_after(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr("security.limits.time.time", lambda: clock["now"])
    client = _fake_client()
    limiter = RedisRateLimiter(per_minute=3, window_seconds=60, client=client)

    assert [asyncio.run(limiter.allow("u1"))[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = asyncio.run(limiter.allow("u1"))
    assert allowed is False
    assert 0 < retry_after <= 60

    # 窗口滑过后恢复
    clock["now"] += 61
    assert asyncio.run(limiter.allow("u1"))[0] is True


def test_redis_limiter_shares_across_instances(monkeypatch):
    clock = {"now": 500.0}
    monkeypatch.setattr("security.limits.time.time", lambda: clock["now"])
    client = _fake_client()
    a = RedisRateLimiter(per_minute=2, window_seconds=60, client=client)
    b = RedisRateLimiter(per_minute=2, window_seconds=60, client=client)
    assert asyncio.run(a.allow("shared"))[0] is True
    assert asyncio.run(b.allow("shared"))[0] is True   # 另一副本共享同一配额
    assert asyncio.run(a.allow("shared"))[0] is False


def test_redis_limiter_degrades_on_error(monkeypatch):
    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    limiter = RedisRateLimiter(per_minute=1, client=Broken())
    assert asyncio.run(limiter.allow("x"))[0] is True
    assert asyncio.run(limiter.allow("x"))[0] is False  # 降级后仍按进程内配额工作

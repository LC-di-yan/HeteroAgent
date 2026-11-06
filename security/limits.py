"""限流与请求体大小控制（T-P0-07）。"""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """滑动窗口限流：每 principal 每分钟 N 次。

    - 单进程内存实现，够本地/单副本用；多副本需换 Redis 计数（P2）。
    - key 数量有上限，避免被随机 key 打爆内存。
    """

    def __init__(self, per_minute: int = 60, max_keys: int = 10_000, window_seconds: float = 60.0):
        self.per_minute = max(1, per_minute)
        self.window = window_seconds
        self.max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """返回 (是否放行, 需等待秒数)。"""
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

    def _evict_oldest(self, now: float) -> None:
        cutoff = now - self.window
        stale = [k for k, b in self._buckets.items() if not b or b[-1] <= cutoff]
        for k in stale[: max(1, len(stale))]:
            self._buckets.pop(k, None)
        if len(self._buckets) >= self.max_keys:  # 仍然满 → 丢最早插入的一批
            for k in list(self._buckets)[: self.max_keys // 10 + 1]:
                self._buckets.pop(k, None)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)

"""用量预算（T-P3-04 的成本护栏）。

目标：给每个用户/租户一个**硬上限**，超了直接拒绝，避免一次错误的重试循环
把当日预算烧光（成本失控最常见的原因不是单价，而是没有上限）。

口径：自然日（UTC）窗口，进程内计数；多副本时可替换为 Redis 计数器。
0 表示不限制。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class BudgetStatus:
    allowed: bool
    remaining_requests: int | None
    remaining_tokens: int | None
    reset_at: float
    reason: str = ""

    def headers(self) -> dict[str, str]:
        out = {"X-Budget-Reset": str(int(self.reset_at))}
        if self.remaining_requests is not None:
            out["X-Budget-Remaining-Requests"] = str(self.remaining_requests)
        if self.remaining_tokens is not None:
            out["X-Budget-Remaining-Tokens"] = str(self.remaining_tokens)
        return out


class TenantBudget:
    """按 (subject, 自然日) 计数的请求与 token 配额。"""

    def __init__(
        self,
        *,
        daily_requests: int = 0,
        daily_tokens: int = 0,
        clock: callable[[], float] | None = None,
    ) -> None:
        self.daily_requests = max(0, int(daily_requests))
        self.daily_tokens = max(0, int(daily_tokens))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        # (subject, day) -> {"requests": int, "tokens": int}
        self._usage: dict[tuple[str, str], dict[str, int]] = {}

    # ── 内部 ──

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _day_of(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def _reset_at(self, ts: float) -> float:
        return (int(ts) // 86400 + 1) * 86400.0  # 下一个 UTC 零点

    def _bucket(self, subject: str, ts: float) -> dict[str, int]:
        key = (subject, self._day_of(ts))
        bucket = self._usage.get(key)
        if bucket is None:
            bucket = {"requests": 0, "tokens": 0}
            self._usage[key] = bucket
            self._gc(ts)
        return bucket

    def _gc(self, ts: float) -> None:
        """只保留今天与昨天的桶，避免长跑进程无限增长。"""
        today = self._day_of(ts)
        keep = {today, self._day_of(ts - 86400)}
        stale = [k for k in self._usage if k[1] not in keep]
        for k in stale:
            self._usage.pop(k, None)

    # ── 公共接口 ──

    def status(self, subject: str) -> BudgetStatus:
        ts = self._now()
        with self._lock:
            bucket = self._bucket(subject, ts)
            remaining_requests = (
                None if self.daily_requests == 0
                else max(0, self.daily_requests - bucket["requests"])
            )
            remaining_tokens = (
                None if self.daily_tokens == 0
                else max(0, self.daily_tokens - bucket["tokens"])
            )
        return BudgetStatus(
            allowed=(remaining_requests is None or remaining_requests > 0),
            remaining_requests=remaining_requests,
            remaining_tokens=remaining_tokens,
            reset_at=self._reset_at(ts),
        )

    def consume(self, subject: str, *, tokens: int = 0) -> BudgetStatus:
        """消耗一次请求（与可选 token）；超限时返回 allowed=False。"""
        ts = self._now()
        with self._lock:
            bucket = self._bucket(subject, ts)
            requests_used = bucket["requests"] + 1
            tokens_used = bucket["tokens"] + max(0, int(tokens))

            over_requests = self.daily_requests > 0 and requests_used > self.daily_requests
            over_tokens = self.daily_tokens > 0 and tokens_used > self.daily_tokens

            if over_requests or over_tokens:
                reason = "请求数超限" if over_requests else "token 超限"
                return BudgetStatus(
                    allowed=False,
                    remaining_requests=None if self.daily_requests == 0
                    else max(0, self.daily_requests - bucket["requests"]),
                    remaining_tokens=None if self.daily_tokens == 0
                    else max(0, self.daily_tokens - bucket["tokens"]),
                    reset_at=self._reset_at(ts),
                    reason=reason,
                )

            bucket["requests"] = requests_used
            bucket["tokens"] = tokens_used
            remaining_requests = (
                None if self.daily_requests == 0 else self.daily_requests - requests_used
            )
            remaining_tokens = (
                None if self.daily_tokens == 0 else self.daily_tokens - tokens_used
            )

        return BudgetStatus(
            allowed=True,
            remaining_requests=remaining_requests,
            remaining_tokens=remaining_tokens,
            reset_at=self._reset_at(ts),
        )

    def add_tokens(self, subject: str, tokens: int) -> BudgetStatus:
        """请求结束后补记 token 消耗（不重复计请求数）。"""
        ts = self._now()
        with self._lock:
            bucket = self._bucket(subject, ts)
            bucket["tokens"] += max(0, int(tokens))
            remaining_requests = (
                None if self.daily_requests == 0 else max(0, self.daily_requests - bucket["requests"])
            )
            remaining_tokens = (
                None if self.daily_tokens == 0 else max(0, self.daily_tokens - bucket["tokens"])
            )
            over = self.daily_tokens > 0 and bucket["tokens"] > self.daily_tokens
        return BudgetStatus(
            allowed=not over,
            remaining_requests=remaining_requests,
            remaining_tokens=remaining_tokens,
            reset_at=self._reset_at(ts),
            reason="token 超限" if over else "",
        )

    def reset(self, subject: str | None = None) -> None:
        with self._lock:
            if subject is None:
                self._usage.clear()
            else:
                for key in [k for k in self._usage if k[0] == subject]:
                    self._usage.pop(key, None)


__all__ = ["TenantBudget", "BudgetStatus"]

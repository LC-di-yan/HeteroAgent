"""熔断器（Circuit Breaker）— 工具调用的客户端侧保护。

从原 mcp/mcp_server.py 内嵌的熔断逻辑抽取为可复用类（缺陷 G12 重构的一部分）：
- 连续失败达到 failure_threshold 次后熔断打开（open），直接拒绝调用；
- 冷却 cooldown_seconds 秒后进入半开（half-open），放行探测调用；
- 探测成功则关闭（closed）；探测失败则重新打开并重新计时。

线程安全：所有状态变更都在 threading.Lock 下完成，可跨线程共享同一实例。
时钟统一通过 time.time() 读取，便于测试用 monkeypatch 模拟冷却。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

# 与原实现保持一致的默认值（Q20）
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


class CircuitBreaker:
    """三态熔断器：closed / open / half-open。

    用法::

        breaker = CircuitBreaker("risk_check")
        if breaker.allow():
            try:
                result = await tool(**args)
                breaker.record_success()
            except Exception:
                breaker.record_failure()
        else:
            ...  # 熔断中，快速失败

    参数：
        name: 熔断器名称（通常为工具名），仅用于展示。
        failure_threshold: 触发熔断的连续失败次数，默认 3。
        cooldown_seconds: 熔断打开后的冷却秒数，默认 60 秒。
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = float(cooldown_seconds)

        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float = 0.0   # 熔断打开时刻（0 表示未打开）
        self._half_open = False        # 冷却结束后的探测窗口
        # 观测指标
        self._total_calls = 0
        self._total_failures = 0
        self._total_rejected = 0

    # ── 内部工具 ────────────────────────────────────────────────

    def _now(self) -> float:
        # 经模块属性读取，测试可 monkeypatch tooling.breaker.time.time
        return time.time()

    # ── 对外 API ────────────────────────────────────────────────

    def allow(self) -> bool:
        """是否放行本次调用。熔断打开且未过冷却期时返回 False。"""
        with self._lock:
            self._total_calls += 1
            if self._half_open:
                return True  # 半开窗口：放行探测调用
            if self._opened_at <= 0.0:
                return True  # closed
            if self._now() - self._opened_at >= self.cooldown_seconds:
                # 冷却结束，进入半开状态并放行一次探测
                self._half_open = True
                return True
            self._total_rejected += 1
            return False

    def record_success(self) -> None:
        """调用成功：关闭熔断（含半开探测成功）。"""
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = 0.0
            self._half_open = False

    def record_failure(self) -> None:
        """调用失败：累计连续失败；半开探测失败或达到阈值则重新打开。"""
        with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            if self._half_open or self._consecutive_failures >= self.failure_threshold:
                self._trip_locked()

    def status(self) -> dict[str, Any]:
        """当前熔断状态快照（供 /api/metrics 展示）。

        冷却期一旦结束即视为 half-open（等待探测），无需先调用 allow()。
        """
        with self._lock:
            now = self._now()
            remaining = 0.0
            if self._opened_at > 0.0:
                elapsed = now - self._opened_at
                if elapsed >= self.cooldown_seconds:
                    state = "half-open"
                else:
                    state = "open"
                    remaining = self.cooldown_seconds - elapsed
            else:
                state = "closed"
            return {
                "name": self.name,
                "state": state,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "cooldown_remaining_s": round(remaining, 3),
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "total_rejected": self._total_rejected,
            }

    def reset(self) -> None:
        """手动复位（测试 / 运维用）。"""
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = 0.0
            self._half_open = False

    # ── 私有 ────────────────────────────────────────────────────

    def _trip_locked(self) -> None:
        """打开熔断（调用方须已持有锁）。"""
        self._opened_at = self._now()
        self._half_open = False
        self._consecutive_failures = 0  # 打开后重新计数（与原实现一致）


# ─── 进程级注册表：每个工具一个熔断器 ─────────────────────────────

_BREAKERS: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    factory: Callable[[str, int, float], CircuitBreaker] | None = None,
) -> CircuitBreaker:
    """按工具名获取熔断器，不存在则创建（默认连续 3 次失败 / 冷却 60 秒）。"""
    with _REGISTRY_LOCK:
        breaker = _BREAKERS.get(name)
        if breaker is None:
            maker = factory or CircuitBreaker
            breaker = maker(name, failure_threshold, cooldown_seconds)
            _BREAKERS[name] = breaker
        return breaker


def all_breaker_statuses() -> dict[str, dict[str, Any]]:
    """全部熔断器状态，对应原 MCPToolServer.get_circuit_status()。"""
    with _REGISTRY_LOCK:
        names = list(_BREAKERS)
    return {name: get_breaker(name).status() for name in names}


def reset_breakers() -> None:
    """清空注册表（测试隔离用）。"""
    with _REGISTRY_LOCK:
        _BREAKERS.clear()

"""安全护栏链（T-P2-08）。

正则是第一道闸（毫秒级、零依赖、可解释），但对拆字/拼音/繁体/多轮诱导仍有余量。
本模块在其上叠加可插拔的"模型分类器"护栏：

    RegexGuardrail  → 复用 security.injection（始终启用）
    ClassifierGuardrail → 任意异步分类器（Llama Prompt Guard / 内部风控 API / 云内容安全）

设计要点：
- 链式短路：前一环命中即返回，不再调用后面的模型（省延迟与成本）；
- 分类器故障时不静默放行：默认标记 degraded 并放行（可用性优先），
  fail_closed=True 时改为拦截（合规优先），由部署方选择；
- 记录耗时与命中类别，便于在指标里观察护栏自身的 P95 与误杀。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 分类器签名：(text) -> (blocked, categories, score)
Classifier = Callable[[str], Awaitable[tuple[bool, list[str], float]]]


@dataclass
class GuardrailVerdict:
    blocked: bool
    categories: list[str] = field(default_factory=list)
    score: float = 0.0
    source: str = ""
    reason: str = ""
    degraded: bool = False
    elapsed_ms: float = 0.0


class Guardrail(Protocol):
    name: str

    async def check(self, text: str) -> GuardrailVerdict: ...


class RegexGuardrail:
    """规则护栏：复用注入检测（与 API 现有行为一致，可单独关闭以做对比）。"""

    name = "regex"

    async def check(self, text: str) -> GuardrailVerdict:
        from security.injection import detect_injection

        start = time.perf_counter()
        findings = detect_injection(text)
        elapsed = (time.perf_counter() - start) * 1000
        return GuardrailVerdict(
            blocked=bool(findings),
            categories=findings,
            score=1.0 if findings else 0.0,
            source=self.name,
            reason="命中注入规则" if findings else "",
            elapsed_ms=elapsed,
        )


class ClassifierGuardrail:
    """模型护栏：包装任意异步分类器，带超时与故障策略。"""

    name = "classifier"

    def __init__(
        self,
        classify: Classifier,
        *,
        timeout_seconds: float = 3.0,
        fail_closed: bool = False,
        threshold: float = 0.5,
    ) -> None:
        self._classify = classify
        self._timeout = timeout_seconds
        self._fail_closed = fail_closed
        self._threshold = threshold

    async def check(self, text: str) -> GuardrailVerdict:
        start = time.perf_counter()
        try:
            blocked, categories, score = await asyncio.wait_for(
                self._classify(text), timeout=self._timeout
            )
        except TimeoutError:
            return self._degraded(start, "分类器超时")
        except Exception as exc:  # noqa: BLE001 - 护栏故障不应把异常抛给调用方
            return self._degraded(start, f"分类器异常: {type(exc).__name__}")

        elapsed = (time.perf_counter() - start) * 1000
        hit = bool(blocked) and float(score) >= self._threshold
        return GuardrailVerdict(
            blocked=hit,
            categories=list(categories or []),
            score=float(score),
            source=self.name,
            reason="模型判定为高风险" if hit else "",
            elapsed_ms=elapsed,
        )

    def _degraded(self, start: float, reason: str) -> GuardrailVerdict:
        logger.warning("护栏降级（%s），fail_closed=%s", reason, self._fail_closed)
        return GuardrailVerdict(
            blocked=self._fail_closed,
            categories=["guardrail_degraded"],
            score=1.0 if self._fail_closed else 0.0,
            source=self.name,
            reason=reason,
            degraded=True,
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )


class HttpClassifierGuardrail(ClassifierGuardrail):
    """HTTP 分类器：POST 文本到外部服务，期望 {"blocked": bool, "categories": [...], "score": float}。"""

    name = "http_classifier"

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 3.0,
        fail_closed: bool = False,
        api_key: str = "",
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key

        async def _call(text: str) -> tuple[bool, list[str], float]:
            import httpx

            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(self._endpoint, json={"text": text}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            return bool(data.get("blocked")), list(data.get("categories") or []), float(data.get("score", 0.0))

        super().__init__(_call, timeout_seconds=timeout_seconds, fail_closed=fail_closed)


class GuardrailChain:
    """按顺序执行，命中即短路；全部通过则放行。"""

    def __init__(self, guardrails: list[Guardrail]) -> None:
        self._guardrails = list(guardrails)

    def __len__(self) -> int:
        return len(self._guardrails)

    async def check(self, text: str) -> GuardrailVerdict:
        degraded = False
        elapsed_total = 0.0
        for guardrail in self._guardrails:
            verdict = await guardrail.check(text)
            elapsed_total += verdict.elapsed_ms
            if verdict.blocked:
                verdict.elapsed_ms = elapsed_total
                return verdict
            degraded = degraded or verdict.degraded
        return GuardrailVerdict(
            blocked=False, source="chain", degraded=degraded, elapsed_ms=elapsed_total
        )


def build_guardrail_chain(settings: Any) -> GuardrailChain | None:
    """按配置构建护栏链；未启用时返回 None（调用方走原有规则检测）。"""
    if not getattr(settings, "guardrail_enabled", False):
        return None

    chain: list[Guardrail] = [RegexGuardrail()]
    endpoint = (getattr(settings, "guardrail_endpoint", "") or "").strip()
    if endpoint:
        chain.append(
            HttpClassifierGuardrail(
                endpoint,
                timeout_seconds=float(getattr(settings, "guardrail_timeout_seconds", 3.0)),
                fail_closed=bool(getattr(settings, "guardrail_fail_closed", False)),
                api_key=getattr(settings, "guardrail_api_key", "") or "",
            )
        )
    logger.info("安全护栏已启用：%d 层", len(chain))
    return GuardrailChain(chain)

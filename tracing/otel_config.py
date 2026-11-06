"""全链路追踪 —— OpenTelemetry 集成（P1 语义对齐的 P0 前置）。

G13 修复：`OTEL_TRACES_SAMPLER` 配置此前声明却从未读取，本版真正生效；
控制台导出改为显式开关，避免生产环境把每个 span 打到 stdout。
"""
from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    _HAS_OTEL = True
except ImportError:  # pragma: no cover
    _HAS_OTEL = False
    ALWAYS_OFF = ALWAYS_ON = ParentBased = TraceIdRatioBased = None  # type: ignore[assignment]

_tracer = None
_global_metrics = None
_SAMPLERS = {
    "always_on": lambda ratio: ALWAYS_ON,
    "always_off": lambda ratio: ALWAYS_OFF,
    "parentbased_always_on": lambda ratio: ParentBased(ALWAYS_ON),
    "parentbased_always_off": lambda ratio: ParentBased(ALWAYS_OFF),
    "traceidratio": lambda ratio: TraceIdRatioBased(ratio),
    "parentbased_traceidratio": lambda ratio: ParentBased(TraceIdRatioBased(ratio)),
}


def build_sampler(name: str, ratio: float = 1.0):
    """把 OTEL_TRACES_SAMPLER 字符串映射为 SDK Sampler；未知值回退 parentbased_always_on。"""
    if not _HAS_OTEL:
        return None
    factory = _SAMPLERS.get((name or "").strip().lower(), _SAMPLERS["parentbased_always_on"])
    try:
        return factory(max(0.0, min(1.0, ratio)))
    except Exception:
        return _SAMPLERS["parentbased_always_on"](1.0)


def set_global_metrics(metrics) -> None:
    global _global_metrics
    _global_metrics = metrics


def init_tracer(
    service_name: str = "heteroagent",
    otlp_endpoint: str | None = None,
    sampler_name: str = "parentbased_always_on",
    sampler_ratio: float = 1.0,
    console_export: bool = False,
    **legacy_kwargs: Any,
) -> None:
    """初始化追踪器。

    - otlp_endpoint 有值 → OTLP 导出；
    - 否则 console_export=True → 控制台导出；
    - 都没有 → 只保留进程内 span（不刷屏）。
    """
    global _tracer
    if not _HAS_OTEL:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource, sampler=build_sampler(sampler_name, sampler_ratio))

    exporter = None
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        except Exception as exc:  # pragma: no cover
            logger.warning("OTLP exporter 初始化失败: %s", exc)
    if exporter is None and console_export:
        exporter = ConsoleSpanExporter()
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    logger.info("OTel 初始化: service=%s sampler=%s exporter=%s",
                service_name, sampler_name, type(exporter).__name__ if exporter else "none")


def get_tracer():
    global _tracer
    if _tracer is None:
        if _HAS_OTEL:
            _tracer = trace.get_tracer("heteroagent")
        else:
            return None
    return _tracer


def trace_agent_call(agent_name: str) -> Callable:
    """为 Agent 方法创建 Span，并上报耗时指标。"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            tracer = get_tracer()
            start = time.perf_counter()

            if tracer is None:
                success = True
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    success = False
                    raise
                finally:
                    if _global_metrics is not None:
                        _global_metrics.record_call(agent_name, (time.perf_counter() - start) * 1000, success)

            span_name = f"agent.{agent_name}.{func.__name__}"
            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("gen_ai.operation.name", "invoke_agent")
                span.set_attribute("gen_ai.agent.name", agent_name)
                span.set_attribute("agent.method", func.__name__)
                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start) * 1000
                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", True)
                    if _global_metrics is not None:
                        _global_metrics.record_call(agent_name, duration_ms, True)
                    return result
                except Exception as exc:
                    duration_ms = (time.perf_counter() - start) * 1000
                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", False)
                    span.record_exception(exc)
                    if _global_metrics is not None:
                        _global_metrics.record_call(agent_name, duration_ms, False)
                    raise

        return wrapper

    return decorator


class AgentMetrics:
    """Agent 调用指标收集器（P1 将替换为 OTel metrics 直方图）。"""

    def __init__(self):
        self._call_counts: dict[str, int] = {}
        self._total_duration: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}
        self._durations: dict[str, list[float]] = {}

    def record_call(self, agent_name: str, duration_ms: float, success: bool) -> None:
        self._call_counts[agent_name] = self._call_counts.get(agent_name, 0) + 1
        self._total_duration[agent_name] = self._total_duration.get(agent_name, 0.0) + duration_ms
        self._durations.setdefault(agent_name, []).append(duration_ms)
        if not success:
            self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return ordered[idx]

    def get_summary(self) -> dict[str, Any]:
        summary = {}
        for name, calls in self._call_counts.items():
            durations = self._durations.get(name, [])
            summary[name] = {
                "total_calls": calls,
                "avg_duration_ms": self._total_duration[name] / calls if calls else 0.0,
                "p50_duration_ms": self._percentile(durations, 0.50),
                "p95_duration_ms": self._percentile(durations, 0.95),
                "error_rate": self._error_counts.get(name, 0) / calls if calls else 0.0,
            }
        return summary

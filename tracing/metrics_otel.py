"""OTel Metrics（T-P2-07）：让指标可以跨副本聚合。

问题：之前的 AgentMetrics 是进程内字典，多副本部署时 /metrics 只能看到"当前副本"。
做法：接入 OTel Metrics SDK，把调用数/耗时/错误/token/成本写成 counter 与 histogram，
通过 OTLP 推给 collector（Prometheus 抓 collector 即可看到全局视图）。

设计取舍：
- 未配置 endpoint 时不创建 provider（避免无谓开销），也不覆盖已有的全局 MeterProvider；
- 保留进程内 AgentMetrics 作为"无 OTEL 时"的兜底，二者可同时启用。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class OtelMetricsBridge:
    """把 AgentMetrics 的记账动作转成 OTel 指标。"""

    def __init__(self, meter: Any) -> None:
        self._calls = meter.create_counter(
            "heteroagent.agent.calls", description="Agent 调用次数"
        )
        self._errors = meter.create_counter(
            "heteroagent.agent.errors", description="Agent 调用失败次数"
        )
        self._duration = meter.create_histogram(
            "heteroagent.agent.duration", unit="ms", description="Agent 调用耗时"
        )
        self._tokens = meter.create_counter(
            "heteroagent.llm.tokens", description="模型 token 数"
        )
        self._cost = meter.create_counter(
            "heteroagent.llm.cost", unit="usd", description="估算模型成本"
        )

    def record_call(self, agent_name: str, duration_ms: float, success: bool) -> None:
        attrs = {"agent": agent_name}
        self._calls.add(1, attrs)
        self._duration.record(float(duration_ms), attrs)
        if not success:
            self._errors.add(1, attrs)

    def record_tokens(
        self, model: str, input_tokens: int, output_tokens: int, cost_usd: float = 0.0
    ) -> None:
        attrs = {"model": model}
        if input_tokens:
            self._tokens.add(int(input_tokens), {**attrs, "direction": "input"})
        if output_tokens:
            self._tokens.add(int(output_tokens), {**attrs, "direction": "output"})
        if cost_usd:
            self._cost.add(float(cost_usd), attrs)


def setup_metrics(
    *,
    service_name: str = "heteroagent",
    otlp_endpoint: str = "",
    interval_ms: int = 60000,
    reader: Any | None = None,
) -> OtelMetricsBridge | None:
    """创建 MeterProvider 并返回桥接器。

    - 传入 reader：用于测试（InMemoryMetricReader），不设置全局 provider；
    - 配置了 otlp_endpoint：周期性推送，并设置为全局 provider；
    - 两者都没有：返回 None（表示不启用），调用方保持进程内指标。
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource

    readers: list[Any] = []
    if reader is not None:
        readers.append(reader)
    elif otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=otlp_endpoint),
                    export_interval_millis=int(interval_ms),
                )
            )
        except Exception as exc:  # 依赖缺失时保持进程内指标
            logger.warning("OTLP 指标导出不可用，保持进程内指标: %s", exc)
            return None
    else:
        return None

    provider = MeterProvider(
        resource=Resource.create({"service.name": service_name}), metric_readers=readers
    )
    if reader is None:  # 生产路径才注册全局 provider
        from opentelemetry import metrics as otel_metrics

        try:
            otel_metrics.set_meter_provider(provider)
        except Exception as exc:  # 已被设置过则复用现有 provider 的 meter
            logger.info("全局 MeterProvider 已存在，跳过覆盖: %s", exc)
            provider = otel_metrics.get_meter_provider()  # type: ignore[assignment]

    meter = provider.get_meter(service_name)
    logger.info("OTel 指标已启用（reader=%s）", type(readers[0]).__name__)
    return OtelMetricsBridge(meter)


__all__ = ["OtelMetricsBridge", "setup_metrics"]

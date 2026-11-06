"""Prometheus 文本格式导出（T-P1-08 的运维侧补完）。

OTLP 适合链路级观测，但集群侧的告警/看板通常抓 Prometheus 文本端点。
本模块把进程内 AgentMetrics 的计数、分位数、token 与成本渲染成 exposition format
（text/plain; version=0.0.4），无需引入 prometheus_client。

指标命名遵循 Prometheus 约定：counter 以 _total 结尾，分位数用 gauge + quantile 标签。
"""
from __future__ import annotations

from typing import Any

DEFAULT_PREFIX = "heteroagent"


def _escape(value: str) -> str:
    """转义标签值：反斜杠、双引号、换行。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _line(name: str, labels: dict[str, Any] | None, value: float | int) -> str:
    if labels:
        body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        return f"{name}{{{body}}} {value}"
    return f"{name} {value}"


def render_prometheus(
    metrics: Any,
    *,
    prefix: str = DEFAULT_PREFIX,
    version: str = "",
) -> str:
    """把 AgentMetrics 渲染为 Prometheus 文本格式。"""
    summary: dict[str, dict[str, Any]] = metrics.get_summary() or {}
    agent_names = sorted(summary)

    out: list[str] = []

    out.append(f"# HELP {prefix}_agent_calls_total Agent 调用次数")
    out.append(f"# TYPE {prefix}_agent_calls_total counter")
    for name in agent_names:
        out.append(_line(f"{prefix}_agent_calls_total", {"agent": name}, summary[name].get("total_calls", 0)))

    out.append(f"# HELP {prefix}_agent_errors_total Agent 调用失败次数")
    out.append(f"# TYPE {prefix}_agent_errors_total counter")
    for name in agent_names:
        entry = summary[name]
        calls = int(entry.get("total_calls", 0) or 0)
        rate = float(entry.get("error_rate", 0.0) or 0.0)
        out.append(_line(f"{prefix}_agent_errors_total", {"agent": name}, int(round(calls * rate))))

    out.append(f"# HELP {prefix}_agent_duration_ms Agent 调用耗时（毫秒）")
    out.append(f"# TYPE {prefix}_agent_duration_ms gauge")
    for name in agent_names:
        entry = summary[name]
        for quantile, key in (("0.5", "p50_duration_ms"), ("0.95", "p95_duration_ms"), ("0.99", "p99_duration_ms")):
            value = round(float(entry.get(key, 0.0) or 0.0), 3)
            out.append(_line(f"{prefix}_agent_duration_ms", {"agent": name, "quantile": quantile}, value))

    out.append(f"# HELP {prefix}_llm_tokens_total 模型 token 数")
    out.append(f"# TYPE {prefix}_llm_tokens_total counter")
    for name in agent_names:
        entry = summary[name]
        out.append(_line(f"{prefix}_llm_tokens_total",
                         {"agent": name, "direction": "input"}, int(entry.get("input_tokens", 0) or 0)))
        out.append(_line(f"{prefix}_llm_tokens_total",
                         {"agent": name, "direction": "output"}, int(entry.get("output_tokens", 0) or 0)))

    out.append(f"# HELP {prefix}_llm_cost_usd_total 估算模型成本（美元）")
    out.append(f"# TYPE {prefix}_llm_cost_usd_total counter")
    for name in agent_names:
        cost = round(float(summary[name].get("estimated_cost_usd", 0.0) or 0.0), 6)
        out.append(_line(f"{prefix}_llm_cost_usd_total", {"agent": name}, cost))

    total_cost = 0.0
    if hasattr(metrics, "total_cost_usd"):
        try:
            total_cost = round(float(metrics.total_cost_usd()), 6)
        except Exception:  # pragma: no cover - 指标故障不应让端点 500
            total_cost = 0.0
    out.append(f"# TYPE {prefix}_cost_usd_total gauge")
    out.append(_line(f"{prefix}_cost_usd_total", None, total_cost))

    if version:
        out.append(f"# TYPE {prefix}_build_info gauge")
        out.append(_line(f"{prefix}_build_info", {"version": version}, 1))

    return "\n".join(out) + "\n"


__all__ = ["render_prometheus"]

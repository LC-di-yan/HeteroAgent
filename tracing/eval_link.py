"""评估结果回挂到 trace（T-P2-04 收尾）。

目标：评估不只是一份 JSON 报告，而是能和产生它的那次调用对上——
把指标以 span 形式写进 trace 流，用 gen_ai.evaluation.* 属性标记，
这样在 Jaeger/Langfuse 里可以按评估名/分数筛选，形成「trace ↔ 评估」闭环。

说明：OTel 的 span 不可变，无法事后给旧 span 追加属性；
因此这里创建一条独立的 gen_ai.evaluation.result span，
并用 evaluation.trace_id 记录被评估的 trace，便于关联查询。
"""
from __future__ import annotations

import logging
from typing import Any

from tracing.otel_config import get_tracer

logger = logging.getLogger(__name__)

EVAL_SPAN_NAME = "gen_ai.evaluation.result"
ATTR_NAME = "gen_ai.evaluation.name"
ATTR_SCORE = "gen_ai.evaluation.score"
ATTR_DATASET = "gen_ai.evaluation.dataset"
ATTR_TRACE = "evaluation.trace_id"


def record_evaluation(
    *,
    name: str,
    score: float,
    dataset: str = "",
    trace_id: str = "",
    attributes: dict[str, Any] | None = None,
) -> bool:
    """写入一条评估事件 span；tracer 不可用时返回 False（不影响主流程）。"""
    tracer = get_tracer()
    if tracer is None:
        return False
    try:
        with tracer.start_as_current_span(EVAL_SPAN_NAME) as span:
            span.set_attribute(ATTR_NAME, str(name))
            span.set_attribute(ATTR_SCORE, float(score))
            if dataset:
                span.set_attribute(ATTR_DATASET, str(dataset))
            if trace_id:
                span.set_attribute(ATTR_TRACE, str(trace_id))
            for key, value in (attributes or {}).items():
                span.set_attribute(key, value)
        return True
    except Exception as exc:  # pragma: no cover - 遥测失败不应影响评估
        logger.warning("评估结果写入 trace 失败: %s", exc)
        return False


def extract_metrics(report: dict[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    """从统一评估报告里抽取可回挂的指标（name, score, attributes）。"""
    out: list[tuple[str, float, dict[str, Any]]] = []

    route = report.get("route") or {}
    if route.get("accuracy") is not None:
        out.append(("route_accuracy", float(route["accuracy"]), {"cases": route.get("total", 0)}))

    comp = report.get("compliance") or {}
    cmetrics = comp.get("metrics") or {}
    for key in ("precision", "recall", "false_positive_rate"):
        if cmetrics.get(key) is not None:
            out.append((f"compliance_{key}", float(cmetrics[key]), {}))

    traj = report.get("trajectory") or {}
    if traj.get("accuracy") is not None:
        out.append(("trajectory_accuracy", float(traj["accuracy"]),
                    {"cases": traj.get("total", 0)}))

    retrieval = report.get("retrieval") or {}
    rmetrics = retrieval.get("metrics") or {}
    for key in ("recall@5", "mrr", "ndcg@5"):
        if rmetrics.get(key) is not None:
            out.append((f"retrieval_{key.replace('@', '_at_')}", float(rmetrics[key]), {}))

    return out


def record_report(report: dict[str, Any], *, dataset: str = "", trace_id: str = "") -> int:
    """把报告里的主要指标逐条回挂，返回成功写入的条数。"""
    written = 0
    for name, score, attrs in extract_metrics(report):
        if record_evaluation(name=name, score=score, dataset=dataset,
                             trace_id=trace_id, attributes=attrs):
            written += 1
    return written


__all__ = ["record_evaluation", "record_report", "extract_metrics", "EVAL_SPAN_NAME"]

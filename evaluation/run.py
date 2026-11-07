"""统一评估入口 + 门禁判定（T-P2-05 的评估环节）。

汇总三个维度并给出可判定的结论：
- route      路由准确率（规则基线或真实模型）
- compliance 合规漏放/误杀（规则引擎，离线可跑）
- retrieval  检索质量（需要向量库与 embedding；离线时跳过并标注）

用法：
    python -m evaluation.run                  # 离线：route + compliance
    python -m evaluation.run --with-retrieval # 追加检索评估（需可用 embedding）
    python -m evaluation.run --with-trajectory # 追加执行轨迹（离线）
    python -m evaluation.run --out report.json --json
退出码：0 = 全部达标；1 = 有维度低于阈值（供 CI 使用）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from evaluation.compliance_eval import evaluate_compliance
from evaluation.route_eval import evaluate_routes, keyword_baseline_router


@dataclass
class Thresholds:
    """门禁阈值。当前值来自本轮实测基线，团队可按风险调整。"""

    route_accuracy_min: float = 0.80
    compliance_fn_max: int = 0          # 漏放：一条都不允许
    compliance_fpr_max: float = 0.05    # 误杀率上限 5%
    retrieval_recall_at_5_min: float = 0.80
    retrieval_mrr_min: float = 0.60
    trajectory_accuracy_min: float = 0.99


DEFAULT_THRESHOLDS = Thresholds()


def _check(report: dict, thresholds: Thresholds) -> list[str]:
    violations: list[str] = []

    route = report.get("route") or {}
    if route.get("accuracy") is not None and route["accuracy"] < thresholds.route_accuracy_min:
        violations.append(
            f"路由准确率 {route['accuracy']} < 阈值 {thresholds.route_accuracy_min}"
        )

    comp = report.get("compliance") or {}
    metrics = comp.get("metrics") or {}
    confusion = comp.get("confusion") or {}
    if confusion.get("fn", 0) > thresholds.compliance_fn_max:
        violations.append(f"合规漏放 {confusion['fn']} 条 > 上限 {thresholds.compliance_fn_max}")
    fpr = metrics.get("false_positive_rate")
    if fpr is not None and fpr > thresholds.compliance_fpr_max:
        violations.append(f"合规误杀率 {fpr} > 阈值 {thresholds.compliance_fpr_max}")

    trajectory = report.get("trajectory") or {}
    if trajectory.get("accuracy") is not None and trajectory["accuracy"] < thresholds.trajectory_accuracy_min:
        violations.append(
            f"轨迹准确率 {trajectory['accuracy']} < 阈值 {thresholds.trajectory_accuracy_min}"
        )

    retrieval = report.get("retrieval")
    if retrieval and retrieval.get("metrics"):
        rm = retrieval["metrics"]
        if rm.get("recall@5") is not None and rm["recall@5"] < thresholds.retrieval_recall_at_5_min:
            violations.append(f"Recall@5 {rm['recall@5']} < 阈值 {thresholds.retrieval_recall_at_5_min}")
        if rm.get("mrr") is not None and rm["mrr"] < thresholds.retrieval_mrr_min:
            violations.append(f"MRR {rm['mrr']} < 阈值 {thresholds.retrieval_mrr_min}")

    return violations


async def _trajectory_report() -> dict:
    """用演示模式图跑轨迹用例（完全离线，验证图接线是否被改坏）。"""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from evaluation.trajectory_eval import _build_demo_graph, evaluate_trajectories
    from tracing.otel_config import install_provider, reset_tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    install_provider(provider)
    try:
        graph = _build_demo_graph()
        report = await evaluate_trajectories(graph, exporter=exporter)
    except Exception as exc:  # 轨迹评估失败不应拖垮整份报告
        return {"total": 0, "accuracy": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        reset_tracing()
    return report.to_dict()


async def build_report(*, with_retrieval: bool = False, with_trajectory: bool = False,
                       llm=None, memory=None) -> dict:
    from agents.compliance_checker import ComplianceCheckerAgent

    route = (await evaluate_routes(keyword_baseline_router)).to_dict()
    compliance = (await evaluate_compliance(ComplianceCheckerAgent(llm=None))).to_dict()

    retrieval = None
    if with_retrieval:
        if memory is None:
            retrieval = {"skipped": True, "reason": "未提供向量库（需要 embedding 凭据）"}
        else:
            from evaluation.evaluator import Evaluator, GoldenSet

            evaluator = Evaluator(llm=llm, long_term_memory=memory)
            try:
                retrieval = await evaluator.evaluate(GoldenSet.default())
            except Exception as exc:  # 检索不可用不应让整份报告失败
                retrieval = {"skipped": True, "reason": f"{type(exc).__name__}: {exc}"}

    trajectory = (await _trajectory_report()) if with_trajectory else None

    return {
        "route": route,
        "compliance": compliance,
        "retrieval": retrieval,
        "trajectory": trajectory,
        "thresholds": DEFAULT_THRESHOLDS.__dict__,
    }


async def _main(args) -> int:  # noqa: ANN001
    memory = None
    if args.with_retrieval:
        try:
            from memory.long_term import create_vector_store

            memory = create_vector_store()
        except Exception as exc:  # pragma: no cover - 依赖环境
            print(f"[warn] 向量库不可用，跳过检索评估: {exc}", file=sys.stderr)

    report = await build_report(with_retrieval=args.with_retrieval,
                                with_trajectory=args.with_trajectory, memory=memory)

    if getattr(args, "emit_eval_events", False):
        from tracing.eval_link import record_report

        written = record_report(report, dataset=getattr(args, "dataset", "") or "default")
        report["evaluation_events"] = written
    violations = _check(report, DEFAULT_THRESHOLDS)
    report["violations"] = violations
    report["passed"] = not violations

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text if args.json else _human_summary(report, violations))
    return 0 if not violations else 1


def _human_summary(report: dict, violations: list[str]) -> str:
    lines = []
    route = report["route"]
    lines.append(f"路由   准确率 {route['accuracy']:.2%} ({route['correct']}/{route['total']})")
    comp = report["compliance"]
    m, c = comp["metrics"], comp["confusion"]
    lines.append(
        f"合规   漏放 {c['fn']} | 误杀 {c['fp']} | precision {m['precision']} | recall {m['recall']}"
    )
    trajectory = report.get("trajectory")
    if trajectory and trajectory.get("accuracy") is not None:
        lines.append(
            f"轨迹   准确率 {trajectory['accuracy']:.2%} "
            f"({trajectory.get('passed', 0)}/{trajectory.get('total', 0)})"
        )
    retrieval = report.get("retrieval")
    if retrieval and retrieval.get("metrics"):
        rm = retrieval["metrics"]
        lines.append(f"检索   Recall@5 {rm.get('recall@5')} | MRR {rm.get('mrr')} | nDCG@5 {rm.get('ndcg@5')}")
    elif retrieval:
        lines.append(f"检索   跳过（{retrieval.get('reason', '未启用')}）")
    lines.append("")
    lines.append("门禁：通过 ✅" if not violations else "门禁：未通过 ❌")
    for v in violations:
        lines.append(f"  - {v}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="HeteroAgent 统一评估")
    parser.add_argument("--with-retrieval", action="store_true", help="追加检索维度（需向量库）")
    parser.add_argument("--with-trajectory", action="store_true",
                        help="追加执行轨迹维度（离线，跑演示模式图）")
    parser.add_argument("--out", default="", help="把 JSON 报告写入文件")
    parser.add_argument("--json", action="store_true", help="直接输出 JSON")
    parser.add_argument("--emit-eval-events", action="store_true",
                        help="把指标以 gen_ai.evaluation.result span 写入 trace 流")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()

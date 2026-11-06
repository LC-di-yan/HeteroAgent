"""检索置信度阈值校准（T-P2-01 / T-P0-05 后续）。

背景：confidence 阈值的语义是"最高检索分数低于它 → 转人工"。
该阈值必须由**真实分数分布**决定，而不是拍脑袋写 0.05 或 0.30。

本工具在标注集上收集两类分数：
- hit：知识库确实有答案的用例（期望被检索到）
- miss：out_of_scope 用例（期望转人工）

然后扫描候选阈值，给出在标注集上准确率最高的取值、分离边界与代价。

用法：
    python -m evaluation.calibrate --threshold 0.30
    python -m evaluation.calibrate --json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field


@dataclass
class Calibration:
    suggested_threshold: float
    accuracy: float
    hit_count: int
    miss_count: int
    hit_scores: list[float] = field(default_factory=list)
    miss_scores: list[float] = field(default_factory=list)
    margin: float | None = None          # min(hit) - max(miss)，>0 表示完全可分
    current_threshold: float | None = None
    current_accuracy: float | None = None
    curve: list[dict] = field(default_factory=list)

    @staticmethod
    def _summary(values: list[float]) -> dict:
        if not values:
            return {"count": 0}
        ordered = sorted(values)
        n = len(ordered)
        return {
            "count": n,
            "min": round(ordered[0], 4),
            "p50": round(ordered[n // 2], 4),
            "p90": round(ordered[min(n - 1, int(n * 0.9))], 4),
            "max": round(ordered[-1], 4),
        }

    def to_dict(self) -> dict:
        return {
            "suggested_threshold": round(self.suggested_threshold, 4),
            "accuracy": round(self.accuracy, 4),
            "hit": self._summary(self.hit_scores),
            "miss": self._summary(self.miss_scores),
            "margin": None if self.margin is None else round(self.margin, 4),
            "current": {
                "threshold": self.current_threshold,
                "accuracy": None if self.current_accuracy is None else round(self.current_accuracy, 4),
            },
            "curve": self.curve,
        }


def _evaluate_at(threshold: float, hit_scores: list[float], miss_scores: list[float]) -> dict:
    tp = sum(1 for s in hit_scores if s >= threshold)
    fn = len(hit_scores) - tp
    fp = sum(1 for s in miss_scores if s >= threshold)
    tn = len(miss_scores) - fp
    total = tp + fn + fp + tn
    accuracy = (tp + tn) / total if total else 0.0
    return {"threshold": round(threshold, 4), "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "accuracy": round(accuracy, 4)}


def calibrate(
    hit_scores: list[float],
    miss_scores: list[float],
    *,
    current_threshold: float | None = None,
) -> Calibration:
    """扫描候选阈值，选准确率最高者；并列时优先"落在两类之间的空隙中部"。"""
    if not hit_scores and not miss_scores:
        raise ValueError("没有可用的分数样本")

    candidates = sorted({0.0, 1.0, *[round(s, 4) for s in hit_scores + miss_scores]})
    curve = [_evaluate_at(t, hit_scores, miss_scores) for t in candidates]
    best_acc = max(point["accuracy"] for point in curve)
    best = [point for point in curve if point["accuracy"] == best_acc]

    margin = (min(hit_scores) - max(miss_scores)) if (hit_scores and miss_scores) else None
    if len(best) == 1:
        chosen = best[0]
    elif margin is not None and margin > 0:
        midpoint = (min(hit_scores) + max(miss_scores)) / 2
        chosen = min(best, key=lambda p: abs(p["threshold"] - midpoint))
    else:
        chosen = best[len(best) // 2]

    current = None
    if current_threshold is not None:
        current = _evaluate_at(current_threshold, hit_scores, miss_scores)["accuracy"]

    return Calibration(
        suggested_threshold=chosen["threshold"],
        accuracy=chosen["accuracy"],
        hit_count=len(hit_scores),
        miss_count=len(miss_scores),
        hit_scores=sorted(hit_scores),
        miss_scores=sorted(miss_scores),
        margin=margin,
        current_threshold=current_threshold,
        current_accuracy=current,
        curve=curve,
    )


def collect_scores(memory, cases, top_k: int = 5) -> tuple[list[float], list[float]]:
    """跑标注集，收集'最高检索分数'并按是否应命中分组。"""
    hit_scores: list[float] = []
    miss_scores: list[float] = []
    for case in cases:
        results = memory.search(case.query, top_k=top_k) or []
        best = max((float(doc.get("score", 0.0) or 0.0) for doc in results), default=0.0)
        source = getattr(case, "expected_source", "") or ""
        if source == "out_of_scope":
            miss_scores.append(best)
        else:
            hit_scores.append(best)
    return hit_scores, miss_scores


def _main(args) -> int:  # noqa: ANN001
    from evaluation.evaluator import DEFAULT_DATASET_PATH, GoldenSet, load_corpus

    dataset = args.dataset or str(DEFAULT_DATASET_PATH)
    golden = GoldenSet.load(dataset)

    from memory.long_term import create_vector_store

    memory = create_vector_store()
    load_corpus(memory)

    hit_scores, miss_scores = collect_scores(memory, golden.cases, top_k=args.top_k)
    result = calibrate(hit_scores, miss_scores, current_threshold=args.threshold)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        data = result.to_dict()
        print(f"样本：应命中 {data['hit']['count']} | 应转人工 {data['miss']['count']}")
        print(f"应命中分数   min {data['hit'].get('min')} p50 {data['hit'].get('p50')} max {data['hit'].get('max')}")
        print(f"应转人工分数 min {data['miss'].get('min')} p50 {data['miss'].get('p50')} max {data['miss'].get('max')}")
        print(f"建议阈值 {data['suggested_threshold']}（标注集准确率 {data['accuracy']}）")
        if data["current"]["threshold"] is not None:
            print(f"当前阈值 {data['current']['threshold']} 的准确率 {data['current']['accuracy']}")
        if data["margin"] is not None:
            note = "可分" if data["margin"] > 0 else "重叠"
            print(f"两类间隙 {data['margin']}（{note}）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="检索阈值校准")
    parser.add_argument("--dataset", default="", help="Golden Set 路径（默认 evaluation/datasets/v1.jsonl）")
    parser.add_argument("--threshold", type=float, default=None, help="当前配置的阈值，用于对比")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(_main(args))


if __name__ == "__main__":
    main()

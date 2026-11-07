"""检索基准测试台：规模语料下的多维度对比。

回答三个问题：
1. 真实召回能力如何？（Recall@K / MRR / nDCG，按分类拆解）
2. 重排到底有没有用？（同一语料、同一查询，开/关重排 A/B）
3. 阈值该定多少？（正负样本分数分布 + 扫描曲线 + 最优工作点）

与 evaluation/run.py 的区别：那里是"门禁"（过/不过），这里是"体检"（详细指标与归因）。

用法：
    python -m evaluation.retrieval_bench --dataset evaluation/datasets/v2.jsonl
    python -m evaluation.retrieval_bench --rerank-ab --out var/bench.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── 指标 ──

def _hit_sources(doc: dict) -> str:
    return str(doc.get("source", "") or "")


def recall_at_k(ranked: list[dict], expected: str, k: int) -> float:
    return 1.0 if any(_hit_sources(d) == expected for d in ranked[:k]) else 0.0


def reciprocal_rank(ranked: list[dict], expected: str) -> float:
    for i, doc in enumerate(ranked, start=1):
        if _hit_sources(doc) == expected:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[dict], expected: str, k: int) -> float:
    dcg = 0.0
    for i, doc in enumerate(ranked[:k], start=1):
        if _hit_sources(doc) == expected:
            dcg += 1.0 / math.log2(i + 1)
    return dcg  # 单一相关文档，IDCG = 1/log2(2) = 1


@dataclass
class CaseResult:
    case_id: str
    query: str
    expected: str
    category: str
    rank: int              # 命中排名，0 = 未命中
    best_score: float
    top_score: float
    latency_ms: float


@dataclass
class BenchReport:
    label: str
    total: int = 0
    grounded: int = 0
    metrics: dict[str, float] = field(default_factory=dict)
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
    latency: dict[str, float] = field(default_factory=dict)
    hit_scores: list[float] = field(default_factory=list)
    miss_scores: list[float] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "total": self.total,
            "grounded": self.grounded,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "by_category": {
                cat: {k: round(v, 4) for k, v in m.items()}
                for cat, m in self.by_category.items()
            },
            "latency_ms": {k: round(v, 1) for k, v in self.latency.items()},
            "failures": self.failures[:30],
        }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


async def run_bench(
    memory: Any,
    cases: list[Any],
    *,
    label: str = "baseline",
    top_k: int = 10,
    reranker: Any = None,
    rerank_top_k: int = 5,
) -> BenchReport:
    """跑一遍检索，收集指标。reranker 非空时对召回结果重排。"""
    report = BenchReport(label=label, total=len(cases))
    per_case: list[CaseResult] = []
    latencies: list[float] = []

    for case in cases:
        expected = getattr(case, "expected_source", "") or ""
        is_oos = expected == "out_of_scope"
        is_conv = expected == "conversation_general"

        start = time.perf_counter()
        ranked = await memory.asearch(case.query, top_k=top_k)
        if reranker is not None and ranked:
            improved = await reranker.rerank(case.query, ranked, top_k=rerank_top_k)
            if improved is not None:
                ranked = improved
        elapsed = (time.perf_counter() - start) * 1000
        latencies.append(elapsed)

        top_score = float(ranked[0].get("score", 0.0)) if ranked else 0.0
        best_score = max((float(d.get("score", 0.0)) for d in ranked), default=0.0)

        if is_conv:
            continue
        if is_oos:
            report.miss_scores.append(best_score)
            continue

        report.grounded += 1
        rank = 0
        for i, doc in enumerate(ranked, start=1):
            if _hit_sources(doc) == expected:
                rank = i
                break
        category = expected.split("/")[0] if "/" in expected else "root"
        per_case.append(CaseResult(
            case_id=getattr(case, "id", ""), query=case.query, expected=expected,
            category=category, rank=rank, best_score=best_score,
            top_score=top_score, latency_ms=elapsed,
        ))
        report.hit_scores.append(best_score)
        if rank == 0 or rank > 5:
            report.failures.append({
                "id": getattr(case, "id", ""), "query": case.query,
                "expected": expected, "rank": rank,
                "top1": _hit_sources(ranked[0]) if ranked else "",
            })

    # 汇总
    n = max(1, report.grounded)
    ranks = [c.rank for c in per_case]
    report.metrics = {
        "recall@1": sum(1 for r in ranks if r == 1) / n,
        "recall@3": sum(1 for r in ranks if 0 < r <= 3) / n,
        "recall@5": sum(1 for r in ranks if 0 < r <= 5) / n,
        "recall@10": sum(1 for r in ranks if 0 < r <= 10) / n,
        "mrr": sum(1.0 / r for r in ranks if r > 0) / n,
    }
    # nDCG 需要原始排序，用 rank 反推
    report.metrics["ndcg@5"] = sum(
        1.0 / math.log2(r + 1) for r in ranks if 0 < r <= 5
    ) / n

    cats: dict[str, list[int]] = {}
    for c in per_case:
        cats.setdefault(c.category, []).append(c.rank)
    for cat, rs in cats.items():
        cn = max(1, len(rs))
        report.by_category[cat] = {
            "cases": len(rs),
            "recall@1": sum(1 for r in rs if r == 1) / cn,
            "recall@5": sum(1 for r in rs if 0 < r <= 5) / cn,
            "mrr": sum(1.0 / r for r in rs if r > 0) / cn,
        }

    report.latency = {
        "p50": _percentile(latencies, 0.50),
        "p95": _percentile(latencies, 0.95),
        "max": max(latencies) if latencies else 0.0,
        "avg": sum(latencies) / len(latencies) if latencies else 0.0,
    }
    return report


def threshold_sweep(hit_scores: list[float], miss_scores: list[float]) -> dict:
    """扫描阈值，给出最优工作点与分布对比。"""
    if not hit_scores:
        return {"error": "no positive samples"}
    lo = min(hit_scores + miss_scores) if miss_scores else min(hit_scores)
    hi = max(hit_scores + miss_scores)
    best = {"threshold": lo, "accuracy": 0.0, "tp": 0, "tn": 0, "fp": 0, "fn": 0}
    curve = []
    for i in range(101):
        t = lo + (hi - lo) * i / 100
        tp = sum(1 for s in hit_scores if s >= t)
        fn = len(hit_scores) - tp
        fp = sum(1 for s in miss_scores if s >= t)
        tn = len(miss_scores) - fp
        acc = (tp + tn) / max(1, len(hit_scores) + len(miss_scores))
        point = {"threshold": round(t, 4), "accuracy": round(acc, 4),
                 "tp": tp, "fn": fn, "fp": fp, "tn": tn}
        curve.append(point)
        if acc > best["accuracy"]:
            best = point
    return {
        "best": best,
        "hit": {"n": len(hit_scores), "min": round(min(hit_scores), 4),
                "p50": round(_percentile(hit_scores, 0.5), 4),
                "max": round(max(hit_scores), 4)},
        "miss": {"n": len(miss_scores),
                 "min": round(min(miss_scores), 4) if miss_scores else None,
                 "p50": round(_percentile(miss_scores, 0.5), 4) if miss_scores else None,
                 "max": round(max(miss_scores), 4) if miss_scores else None},
        "separable": bool(miss_scores) and min(hit_scores) > max(miss_scores),
        "curve_sample": curve[::10],
    }


async def _main(args: Any) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    from config import Settings
    from evaluation.evaluator import GoldenSet
    from memory.kb_loader import corpus_stats, ingest_corpus, load_documents
    from memory.long_term import create_vector_store
    from memory.rerank_api import build_api_reranker

    settings = Settings()
    docs = load_documents(args.kb)
    stats = corpus_stats(docs)
    print("语料:", json.dumps(stats, ensure_ascii=False))

    golden = GoldenSet.load(args.dataset)
    print("用例:", len(golden.cases))

    memory = create_vector_store(settings=settings)
    ingest_stats = ingest_corpus(memory, docs, chunk_size=args.chunk_size)
    print("入库:", json.dumps(ingest_stats, ensure_ascii=False))

    reports = []
    base = await run_bench(memory, golden.cases, label="vector+bm25", top_k=args.top_k)
    reports.append(base)
    print("\n[基线] " + json.dumps(base.to_dict()["metrics"], ensure_ascii=False))

    if args.rerank_ab:
        rr = build_api_reranker(settings)
        if rr is None:
            print("未配置 API 重排器，跳过 A/B")
        else:
            reranked = await run_bench(
                memory, golden.cases, label="+rerank", top_k=args.top_k, reranker=rr
            )
            reports.append(reranked)
            print("[重排] " + json.dumps(reranked.to_dict()["metrics"], ensure_ascii=False))

    sweep = threshold_sweep(base.hit_scores, base.miss_scores)
    payload = {
        "corpus": stats,
        "ingest": ingest_stats,
        "dataset": args.dataset,
        "cases": len(golden.cases),
        "reports": [r.to_dict() for r in reports],
        "threshold": sweep,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("\n报告已写入:", args.out)
    print("\n阈值扫描:", json.dumps(
        {k: v for k, v in sweep.items() if k != "curve_sample"}, ensure_ascii=False
    ))
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="检索基准测试")
    parser.add_argument("--kb", default="knowledge_base")
    parser.add_argument("--dataset", default="evaluation/datasets/v2.jsonl")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--chunk-size", type=int, default=512)
    parser.add_argument("--rerank-ab", action="store_true", help="对比开/关重排")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()

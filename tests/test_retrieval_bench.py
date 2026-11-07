"""检索基准指标测试（手算对表，离线）。"""
from __future__ import annotations

import asyncio

import pytest

from evaluation.build_golden import _parse_queries, extract_keywords
from evaluation.retrieval_bench import (
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
    run_bench,
    threshold_sweep,
)


def _docs(*sources):
    return [{"source": s, "score": 0.9 - i * 0.1} for i, s in enumerate(sources)]


def test_recall_and_rr_by_position():
    ranked = _docs("a.md", "b.md", "c.md")
    assert recall_at_k(ranked, "b.md", 1) == 0.0
    assert recall_at_k(ranked, "b.md", 3) == 1.0
    assert reciprocal_rank(ranked, "b.md") == pytest.approx(0.5)
    assert reciprocal_rank(ranked, "zzz.md") == 0.0


def test_ndcg_hand_computed():
    ranked = _docs("a.md", "b.md", "c.md")
    # 命中在第 2 位：1/log2(3) ≈ 0.6309
    assert ndcg_at_k(ranked, "b.md", 5) == pytest.approx(0.6309297, abs=1e-6)
    assert ndcg_at_k(ranked, "a.md", 5) == pytest.approx(1.0)
    assert ndcg_at_k(ranked, "zzz.md", 5) == 0.0


def test_threshold_sweep_separable():
    out = threshold_sweep([0.8, 0.9], [0.1, 0.2])
    assert out["separable"] is True
    assert out["best"]["accuracy"] == 1.0


def test_threshold_sweep_overlap():
    out = threshold_sweep([0.3, 0.8], [0.2, 0.5])
    assert out["separable"] is False
    assert 0.0 < out["best"]["accuracy"] <= 1.0


def test_threshold_sweep_no_positives():
    assert "error" in threshold_sweep([], [0.1])


def test_extract_keywords_are_real_substrings():
    content = "起投金额10000元，封闭期90天，风险等级R2，3-5个工作日到账。"
    kws = extract_keywords(content, "稳健理财说明")
    assert kws
    for kw in kws:
        assert kw in content or kw in "稳健理财说明"


def test_parse_queries_handles_fences_and_plain():
    fence = chr(96) * 3
    assert _parse_queries(fence + 'json\n["q1","q2"]\n' + fence) == ["q1", "q2"]
    assert _parse_queries('["a","b"]') == ["a", "b"]
    assert _parse_queries('{"queries": ["x"]}') == ["x"]


class _Case:
    def __init__(self, query, source, cid="c1"):
        self.query = query
        self.expected_source = source
        self.id = cid


class _Mem:
    def __init__(self, mapping):
        self.mapping = mapping

    async def asearch(self, query, top_k=10):
        return self.mapping.get(query, [])


def test_run_bench_splits_grounded_and_oos():
    cases = [
        _Case("q1", "products/p01.md", "c1"),
        _Case("q2", "out_of_scope", "c2"),
        _Case("你好", "conversation_general", "c3"),
    ]
    mem = _Mem({
        "q1": [{"source": "products/p01.md", "score": 0.8}],
        "q2": [{"source": "products/p99.md", "score": 0.4}],
        "你好": [],
    })
    report = asyncio.run(run_bench(mem, cases, label="t"))
    assert report.total == 3
    assert report.grounded == 1          # 寒暄与 oos 都不进召回分母
    assert report.metrics["recall@1"] == 1.0
    assert report.miss_scores == [0.4]
    assert report.by_category["products"]["cases"] == 1


def test_run_bench_records_failure_when_not_retrieved():
    cases = [_Case("q", "products/p01.md")]
    mem = _Mem({"q": [{"source": "other.md", "score": 0.5}]})
    report = asyncio.run(run_bench(mem, cases))
    assert report.metrics["recall@5"] == 0.0
    assert len(report.failures) == 1
    assert report.failures[0]["rank"] == 0

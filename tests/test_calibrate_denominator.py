"""校准分母口径回归（曾把寒暄类算成"应命中"，导致阈值建议偏低）。"""
from __future__ import annotations

from evaluation.calibrate import collect_scores
from evaluation.evaluator import EvalCase


class _Memory:
    def __init__(self, scores):
        self.scores = scores
        self.queries = []

    def search(self, query, top_k=5):
        self.queries.append(query)
        score = self.scores.get(query, 0.0)
        return [{"id": "d", "content": "x", "source": "s", "score": score}] if score else []


def test_conversational_cases_excluded_from_calibration():
    """寒暄类不该匹配知识文档，计入应命中会人为制造分数重叠。"""
    cases = [
        EvalCase(query="收益率多少", expected_content="产品", expected_source="product_faq.md"),
        EvalCase(query="你好", expected_content="", expected_source="conversation_general"),
        EvalCase(query="谢谢", expected_content="", expected_source="conversation_general"),
        EvalCase(query="股市行情", expected_content="", expected_source="out_of_scope"),
    ]
    mem = _Memory({"收益率多少": 0.8, "你好": 0.05, "谢谢": 0.03, "股市行情": 0.4})
    hits, misses = collect_scores(mem, cases)

    assert hits == [0.8]           # 只有 grounded 用例
    assert misses == [0.4]         # out_of_scope 保留为负样本
    assert "你好" not in mem.queries  # 寒暄类连检索都不必跑
    assert "谢谢" not in mem.queries


def test_out_of_scope_kept_as_negative():
    """out_of_scope 的 is_grounded 也是 False，但不能因此被跳过。"""
    cases = [
        EvalCase(query="退款政策", expected_content="退款", expected_source="refund_policy.md"),
        EvalCase(query="火星天气", expected_content="", expected_source="out_of_scope"),
    ]
    mem = _Memory({"退款政策": 0.75, "火星天气": 0.35})
    hits, misses = collect_scores(mem, cases)
    assert len(hits) == 1 and len(misses) == 1


def test_legacy_cases_without_helpers_still_work():
    """兼容没有 is_conversational 属性的旧用例对象。"""
    class _Legacy:
        def __init__(self, query, source):
            self.query = query
            self.expected_source = source

    cases = [_Legacy("q1", "product_faq.md"), _Legacy("q2", "out_of_scope")]
    mem = _Memory({"q1": 0.9, "q2": 0.2})
    hits, misses = collect_scores(mem, cases)
    assert hits == [0.9] and misses == [0.2]

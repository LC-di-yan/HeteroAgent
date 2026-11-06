"""合规质量评估测试（T-P2-04 合规维度）。"""
from __future__ import annotations

import asyncio

from agents.compliance_checker import ComplianceCheckerAgent
from evaluation.compliance_eval import (
    LABELED_CASES,
    ComplianceCase,
    evaluate_compliance,
)


class _AlwaysBlock:
    async def rule_check(self, content):  # noqa: ANN001
        class _R:
            passed = False
            violations = ["always"]
            sanitized_content = content
        return _R()


def test_labeled_set_has_both_classes_and_categories():
    assert sum(1 for c in LABELED_CASES if c.should_block) >= 8
    assert sum(1 for c in LABELED_CASES if not c.should_block) >= 10
    assert {c.category for c in LABELED_CASES} >= {"forbidden_term", "pii", "negation", "benign", "order_id"}


def test_rule_engine_meets_current_baseline():
    report = asyncio.run(evaluate_compliance(ComplianceCheckerAgent(llm=None)))
    payload = report.to_dict()
    assert payload["confusion"]["fn"] == 0, payload["failures"]   # 不漏放
    assert payload["confusion"]["fp"] == 0, payload["failures"]   # 不误杀（订单号/流水号）
    assert payload["metrics"]["masking_accuracy"] == 1.0
    assert payload["metrics"]["f1"] == 1.0


def test_all_block_checker_produces_false_positives():
    benign = [c for c in LABELED_CASES if not c.should_block]
    report = asyncio.run(evaluate_compliance(_AlwaysBlock(), cases=benign))
    payload = report.to_dict()
    assert payload["confusion"]["fp"] > 0
    assert payload["metrics"]["false_positive_rate"] > 0
    assert any(f["type"] == "误杀" for f in payload["failures"])


def test_custom_case_with_wrong_label_is_reported():
    case = ComplianceCase("x1", "理财产品A年化收益率为 3.5%", should_block=False, category="benign")
    report = asyncio.run(evaluate_compliance(_AlwaysBlock(), cases=[case]))
    assert report.fp == 1 and report.tp == 0
    assert report.to_dict()["metrics"]["precision"] == 0.0

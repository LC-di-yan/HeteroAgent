"""合规质量评估（T-P2-04 的合规维度）。

度量规则引擎在标注集上的表现：
- 漏放率（false negative rate）：应拦截却放行
- 误杀率（false positive rate）：合规表述被拦截
- 脱敏正确率：PII 是否被替换

LLM 深审不在本模块度量（需要真实模型与人工复核），但接口允许传入带 llm 的 Agent。
用法：
    python -m evaluation.compliance_eval
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field


@dataclass
class ComplianceCase:
    case_id: str
    text: str
    should_block: bool
    category: str  # forbidden_term / pii / negation / benign / order_id
    expect_masked: bool = False


LABELED_CASES: list[ComplianceCase] = [
    # 应拦截：违规金融用语
    ComplianceCase("c01", "我们保证收益，稳赚不赔", True, "forbidden_term"),
    ComplianceCase("c02", "本产品零风险，保本保息", True, "forbidden_term"),
    ComplianceCase("c03", "内部消息显示下周必涨", True, "forbidden_term"),
    ComplianceCase("c04", "承诺回报 20% 以上", True, "forbidden_term"),
    # 应拦截：PII
    ComplianceCase("c05", "请联系我 13812345678", True, "pii", expect_masked=True),
    ComplianceCase("c06", "身份证 110101199003078937", True, "pii", expect_masked=True),
    ComplianceCase("c07", "卡号 4111111111111111 请查收", True, "pii", expect_masked=True),
    ComplianceCase("c08", "邮箱 john@example.com", True, "pii", expect_masked=True),
    # 不应拦截：否定/警示表述
    ComplianceCase("c09", "本产品不能保证收益，投资有风险", False, "negation"),
    ComplianceCase("c10", "请勿相信任何零风险的承诺", False, "negation"),
    ComplianceCase("c11", "理财非存款，不保本保息", False, "negation"),
    # 不应拦截：正常业务文本
    ComplianceCase("c12", "理财产品A年化收益率为 3.5%-5.2%", False, "benign"),
    ComplianceCase("c13", "退款将在 3-5 个工作日内原路退回", False, "benign"),
    ComplianceCase("c14", "开户需要身份证原件与视频认证", False, "benign"),
    ComplianceCase("c15", "您可以在 App 内查看持仓明细", False, "benign"),
    # 不应拦截：订单号/长数字（误杀高风险区）
    ComplianceCase("c16", "订单号 TK-20261106-ABC123 已受理", False, "order_id"),
    ComplianceCase("c17", "流水号 202611061234567890", False, "order_id"),
    ComplianceCase("c18", "合同编号 1234567890123456789", False, "order_id"),
    # 边界：否定词距离过远 → 应拦截
    ComplianceCase("c19", "风险提示写在别处。我们保证收益", True, "forbidden_term"),
    # 边界：合规免责声明
    ComplianceCase("c20", "以上信息仅供参考，不构成投资建议，理财有风险", False, "benign"),
]


@dataclass
class ComplianceReport:
    total: int
    tp: int
    fp: int
    tn: int
    fn: int
    precision: float
    recall: float
    f1: float
    false_positive_rate: float
    accuracy: float
    masking_ok: int
    masking_total: int
    per_category: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "confusion": {"tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn},
            "metrics": {
                "precision": round(self.precision, 4),
                "recall": round(self.recall, 4),
                "f1": round(self.f1, 4),
                "false_positive_rate": round(self.false_positive_rate, 4),
                "accuracy": round(self.accuracy, 4),
                "masking_accuracy": round(self.masking_ok / self.masking_total, 4)
                if self.masking_total else None,
            },
            "per_category": self.per_category,
            "failures": self.failures,
        }


async def evaluate_compliance(checker, cases: list[ComplianceCase] | None = None) -> ComplianceReport:
    """用规则引擎跑标注集；checker 需提供 async rule_check(content) 与 mask_pii。"""
    cases = cases or LABELED_CASES
    tp = fp = tn = fn = 0
    masking_ok = masking_total = 0
    per_category: dict[str, dict[str, int]] = {}
    failures: list[dict] = []

    for case in cases:
        result = await checker.rule_check(case.text)
        blocked = not result.passed
        stats = per_category.setdefault(case.category, {"correct": 0, "total": 0})
        stats["total"] += 1

        if case.should_block and blocked:
            tp += 1
            stats["correct"] += 1
        elif case.should_block and not blocked:
            fn += 1
            failures.append({"id": case.case_id, "type": "漏放", "text": case.text})
        elif not case.should_block and blocked:
            fp += 1
            failures.append({"id": case.case_id, "type": "误杀", "text": case.text,
                             "violations": result.violations})
        else:
            tn += 1
            stats["correct"] += 1

        if case.expect_masked:
            masking_total += 1
            masked = result.sanitized_content or ""
            if masked != case.text and "****" in masked or "***" in masked:
                masking_ok += 1
            else:
                failures.append({"id": case.case_id, "type": "脱敏失败", "text": case.text})

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    accuracy = (tp + tn) / len(cases)

    return ComplianceReport(
        total=len(cases), tp=tp, fp=fp, tn=tn, fn=fn,
        precision=precision, recall=recall, f1=f1,
        false_positive_rate=fpr, accuracy=accuracy,
        masking_ok=masking_ok, masking_total=masking_total,
        per_category=per_category, failures=failures,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="合规规则引擎评估")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    from agents.compliance_checker import ComplianceCheckerAgent

    checker = ComplianceCheckerAgent(llm=None)  # 只跑规则引擎，不触网
    report = asyncio.run(evaluate_compliance(checker))
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        m = payload["metrics"]
        print(f"样本 {payload['total']} | 漏放 {payload['confusion']['fn']} | 误杀 {payload['confusion']['fp']}")
        print(f"precision={m['precision']} recall={m['recall']} f1={m['f1']} 误杀率={m['false_positive_rate']} 脱敏={m['masking_accuracy']}")
        for f in payload["failures"]:
            print("  ✗", f["type"], f["id"], f["text"][:30])


if __name__ == "__main__":
    main()

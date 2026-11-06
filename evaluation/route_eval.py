"""路由质量评估（T-P2-04 的 trajectory 维度）。

度量 Supervisor 把用户诉求分发给正确子 Agent 的能力：
- 整体准确率、每类 precision/recall/F1、混淆矩阵、失败样例；
- 内置两个路由器：
  * keyword_baseline_router —— 规则基线（无需模型，CI 可跑，用于"LLM 路由至少不该比规则差"）
  * make_llm_router(llm) —— 复用生产的路由提示词与结构化输出

用法：
    python -m evaluation.route_eval                # 规则基线
    python -m evaluation.route_eval --router llm   # 需要 OPENAI_API_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

RouterFn = Callable[[str], Awaitable[str]]

KNOWLEDGE = "knowledge_rag"
TICKET = "ticket_handler"


@dataclass
class RouteCase:
    case_id: str
    query: str
    expected: str  # knowledge_rag / ticket_handler
    note: str = ""


ROUTE_CASES: list[RouteCase] = [
    # 咨询类 → 知识库
    RouteCase("r01", "理财产品A的收益率是多少？", KNOWLEDGE),
    RouteCase("r02", "这个产品有风险吗", KNOWLEDGE),
    RouteCase("r03", "开户需要准备什么材料", KNOWLEDGE),
    RouteCase("r04", "退款政策是怎样的", KNOWLEDGE, "含「退款」字样但问的是政策"),
    RouteCase("r05", "你们的客服电话是多少", KNOWLEDGE),
    RouteCase("r06", "可以介绍一下你们的理财产品吗", KNOWLEDGE),
    RouteCase("r07", "为什么我的收益和预期不一样", KNOWLEDGE),
    RouteCase("r08", "什么是年化收益率", KNOWLEDGE),
    RouteCase("r09", "购买后多久能看到收益", KNOWLEDGE),
    RouteCase("r10", "风险评估问卷怎么填", KNOWLEDGE),
    # 受理类 → 工单
    RouteCase("r11", "我要申请退款", TICKET),
    RouteCase("r12", "帮我办理开户", TICKET),
    RouteCase("r13", "我要投诉你们客服态度", TICKET),
    RouteCase("r14", "理赔怎么申请，我的保单号是 P123", TICKET),
    RouteCase("r15", "查一下工单 TK-20261106-ABC123 的进度", TICKET),
    RouteCase("r16", "我要修改绑定的手机号", TICKET),
    RouteCase("r17", "这笔钱什么时候退给我，已经等了 10 天", TICKET),
    RouteCase("r18", "帮我提交一个账户变更申请", TICKET),
    RouteCase("r19", "我要销户", TICKET),
    RouteCase("r20", "申请理赔需要多久能到账", TICKET, "带「申请」动作意图"),
    # 边界：混合诉求
    RouteCase("r21", "退款要几天到账？顺便帮我提交退款申请", TICKET, "含办理动作"),
    RouteCase("r22", "你们支持哪些退款方式，我想了解下", KNOWLEDGE),
    RouteCase("r23", "我买的理财产品能提前赎回吗", KNOWLEDGE),
    RouteCase("r24", "帮我赎回，全部赎回", TICKET),
    RouteCase("r25", "投诉电话是多少", KNOWLEDGE, "问的是联系方式"),
    RouteCase("r26", "我要投诉", TICKET),
    RouteCase("r27", "收益率 3.5% 是怎么算出来的", KNOWLEDGE),
    RouteCase("r28", "把我去年的对账单发给我", TICKET),
    RouteCase("r29", "忘记交易密码了怎么办", KNOWLEDGE),
    RouteCase("r30", "帮我重置交易密码", TICKET),
]

_ACTION_HINTS = ("我要", "帮我", "申请", "办理", "提交", "投诉", "查询", "查一下", "销户", "重置", "赎回")
_OBJECT_HINTS = ("退款", "开户", "理赔", "工单", "账户", "销户", "对账", "密码", "手机号", "赎回", "投诉")
_QUESTION_HINTS = ("吗", "怎么", "什么", "多少", "如何", "为什么", "介绍", "支持哪些", "是怎样的", "需要")


def keyword_baseline_router_sync(text: str) -> str:
    """规则基线：有动作词 + 业务对象 → 工单；问句/知识词 → 知识库。"""
    normalized = text.strip()
    has_action = any(h in normalized for h in _ACTION_HINTS)
    has_object = any(h in normalized for h in _OBJECT_HINTS)
    is_question = any(h in normalized for h in _QUESTION_HINTS)
    if has_action and has_object and not (is_question and not has_action):
        return TICKET
    if re.search(r"TK-\d{8}-[A-F0-9]{6}", normalized.upper()):
        return TICKET
    return KNOWLEDGE


async def keyword_baseline_router(text: str) -> str:
    return keyword_baseline_router_sync(text)


def make_llm_router(llm) -> RouterFn:
    """复用生产路由逻辑（同一提示词 + 结构化输出 + 文本兜底）。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.llm_utils import run_structured
    from agents.schemas import RouteDecision
    from agents.supervisor import _ROUTE_ALIASES, SUPERVISOR_SYSTEM_PROMPT, parse_route

    async def router(text: str) -> str:
        messages = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            HumanMessage(content=text),
            HumanMessage(content="只返回 knowledge_rag 或 ticket_handler。"),
        ]
        decision = await run_structured(llm, RouteDecision, messages, label="route-eval")
        if decision is not None:
            return _ROUTE_ALIASES.get(decision.target, KNOWLEDGE)
        response = await llm.ainvoke(messages)
        return parse_route(getattr(response, "content", "") or "")

    return router


@dataclass
class RouteReport:
    total: int
    correct: int
    accuracy: float
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
            "per_class": self.per_class,
            "confusion": self.confusion,
            "failures": self.failures,
        }


async def evaluate_routes(router: RouterFn, cases: list[RouteCase] | None = None) -> RouteReport:
    cases = cases or ROUTE_CASES
    labels = (KNOWLEDGE, TICKET)
    confusion = {expected: {pred: 0 for pred in labels} for expected in labels}
    tp = {label: 0 for label in labels}
    fp = {label: 0 for label in labels}
    fn = {label: 0 for label in labels}
    correct = 0
    failures: list[dict] = []

    for case in cases:
        error_name: str | None = None
        try:
            predicted = await router(case.query)
        except Exception as exc:  # 路由器自身故障：记为错误分类，只记一条
            predicted = "<other>"
            error_name = type(exc).__name__
        predicted = predicted if predicted in labels else "<other>"
        confusion.setdefault(case.expected, {}).setdefault(predicted, 0)
        confusion[case.expected][predicted] = confusion[case.expected].get(predicted, 0) + 1

        if predicted == case.expected:
            correct += 1
            tp[case.expected] += 1
        else:
            fn[case.expected] += 1
            if predicted in fp:
                fp[predicted] += 1
            entry = {
                "id": case.case_id, "query": case.query,
                "expected": case.expected, "predicted": predicted,
                "note": case.note,
            }
            if error_name:
                entry["error"] = error_name
            failures.append(entry)

    per_class: dict[str, dict[str, float]] = {}
    for label in labels:
        precision = tp[label] / (tp[label] + fp[label]) if (tp[label] + fp[label]) else 0.0
        recall = tp[label] / (tp[label] + fn[label]) if (tp[label] + fn[label]) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": tp[label] + fn[label],
        }

    return RouteReport(
        total=len(cases), correct=correct, accuracy=correct / len(cases),
        per_class=per_class, confusion=confusion, failures=failures,
    )


async def _main(router_name: str) -> int:
    if router_name == "llm":
        import os

        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model=os.getenv("MODEL_NAME", "gpt-5.1"), temperature=0)
        router = make_llm_router(llm)
    else:
        router = keyword_baseline_router

    report = await evaluate_routes(router)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.accuracy >= 0.8 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="路由质量评估")
    parser.add_argument("--router", choices=["keyword", "llm"], default="keyword")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.router)))


if __name__ == "__main__":
    main()

"""执行轨迹评估（T-P2-04 的 trajectory 维度）。

回答的问题：一次请求实际走了哪些 Agent、调了哪些工具，是否和预期一致？
这比"答案对不对"更早暴露问题——答案可能凑巧正确，但路径已经偏了（例如该查知识库却建了工单）。

实现方式：跑图时用 InMemorySpanExporter 收集 span，按开始时间还原
- 节点序列：invoke_agent <agent>.<method> 的 agent 名
- 工具序列：execute_tool <name> 的工具名

用法：
    python -m evaluation.trajectory_eval          # 用演示模式跑（无需 API Key）
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrajectoryCase:
    case_id: str
    query: str
    expected_nodes: tuple[str, ...]
    expected_tools: tuple[str, ...] = ()
    should_block: bool = False


# 演示模式下确定性的预期路径
CASES: list[TrajectoryCase] = [
    TrajectoryCase(
        "t1", "理财产品A的收益率是多少",
        expected_nodes=("route", "knowledge", "compliance", "gate", "synthesize"),
    ),
    TrajectoryCase(
        "t2", "我要申请退款",
        expected_nodes=("route", "ticket", "compliance", "gate", "synthesize"),
    ),
    TrajectoryCase(
        "t3", "你们能保证收益吗",
        expected_nodes=("route", "knowledge", "compliance", "gate", "synthesize"),
        should_block=True,
    ),
]


@dataclass
class CaseResult:
    case_id: str
    nodes: list[str]
    tools: list[str]
    nodes_match: bool
    tools_match: bool
    blocked_as_expected: bool
    passed: bool
    missing_nodes: list[str] = field(default_factory=list)
    extra_nodes: list[str] = field(default_factory=list)


@dataclass
class TrajectoryReport:
    total: int
    passed: int
    accuracy: float
    cases: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "accuracy": round(self.accuracy, 4),
            "cases": [
                {
                    "id": c.case_id, "nodes": c.nodes, "tools": c.tools,
                    "nodes_match": c.nodes_match, "tools_match": c.tools_match,
                    "blocked_as_expected": c.blocked_as_expected, "passed": c.passed,
                    "missing_nodes": c.missing_nodes, "extra_nodes": c.extra_nodes,
                }
                for c in self.cases
            ],
        }


_PHASE_RULES: tuple[tuple[str, str], ...] = (
    ("supervisor_synthesize", "synthesize"),
    ("supervisor", "route"),
    ("compliance_gate", "gate"),
    ("compliance", "compliance"),
    ("ticket", "ticket"),
    ("rag_", "knowledge"),
    ("knowledge_", "knowledge"),
)


def phase_of(agent: str) -> str:
    """把方法级 span 名归一到「阶段」：route / knowledge / ticket / compliance / gate / synthesize。"""
    for prefix, phase in _PHASE_RULES:
        if agent.startswith(prefix):
            return phase
    return agent


def extract_path(spans: list[Any]) -> tuple[list[str], list[str]]:
    """从 span 列表还原 (阶段序列, 工具序列)，按开始时间排序并折叠连续重复。"""
    ordered = sorted(spans, key=lambda s: getattr(s, "start_time", 0))
    nodes: list[str] = []
    tools: list[str] = []
    for span in ordered:
        name = getattr(span, "name", "")
        attrs = getattr(span, "attributes", {}) or {}
        if name.startswith("execute_tool "):
            tool = attrs.get("gen_ai.tool.name") or name[len("execute_tool "):]
            tools.append(str(tool))
        elif name.startswith("invoke_agent "):
            agent = attrs.get("gen_ai.agent.name")
            if not agent:
                continue
            phase = phase_of(str(agent))
            if not nodes or nodes[-1] != phase:
                nodes.append(phase)
    return nodes, tools


async def evaluate_trajectories(
    graph: Any,
    cases: list[TrajectoryCase] | None = None,
    *,
    exporter: Any = None,
    session_prefix: str = "traj",
) -> TrajectoryReport:
    """逐用例跑图并比对路径；exporter 由调用方提供（需已安装到 TracerProvider）。"""
    cases = cases or CASES
    results: list[CaseResult] = []

    for case in cases:
        if exporter is not None:
            exporter.clear()
        state = {
            "messages": [("user", case.query)],
            "user_id": "traj-user", "session_id": f"{session_prefix}-{case.case_id}",
            "trace_id": case.case_id, "intent": "", "sub_results": {},
            "compliance_passed": True, "final_response": "", "retry_count": 0,
            "approval_id": None, "approval_decision": None,
        }
        out = await graph.ainvoke(state, config={"configurable": {"thread_id": f"{session_prefix}:{case.case_id}"}})
        nodes, tools = extract_path(list(exporter.get_finished_spans()) if exporter else [])

        missing = [n for n in case.expected_nodes if n not in nodes]
        extra = [n for n in nodes if n not in case.expected_nodes]
        nodes_match = tuple(nodes) == case.expected_nodes
        tools_match = set(tools) == set(case.expected_tools)
        blocked = not bool(out.get("compliance_passed", True))
        blocked_as_expected = blocked == case.should_block

        results.append(CaseResult(
            case_id=case.case_id, nodes=nodes, tools=tools,
            nodes_match=nodes_match, tools_match=tools_match,
            blocked_as_expected=blocked_as_expected,
            passed=nodes_match and tools_match and blocked_as_expected,
            missing_nodes=missing, extra_nodes=extra,
        ))

    passed = sum(1 for r in results if r.passed)
    return TrajectoryReport(total=len(results), passed=passed,
                            accuracy=passed / len(results) if results else 0.0,
                            cases=results)


def _build_demo_graph():
    from langgraph.checkpoint.memory import MemorySaver

    from agents.demo_mode import DemoKnowledgeBase, ScriptedChatModel
    from agents.supervisor import create_supervisor_graph
    from config import Settings

    settings = Settings(demo_mode=True, checkpoint_backend="memory")
    return create_supervisor_graph(
        llm=ScriptedChatModel(), settings=settings, long_term_memory=DemoKnowledgeBase(),
        enable_checkpointing=False, checkpointer=MemorySaver(),
    )


async def _main(json_output: bool) -> int:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from tracing.otel_config import install_provider, reset_tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    install_provider(provider)

    try:
        graph = _build_demo_graph()
        report = await evaluate_trajectories(graph, exporter=exporter)
    finally:
        reset_tracing()

    payload = report.to_dict()
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"轨迹用例 {payload['total']} | 通过 {payload['passed']} | 准确率 {payload['accuracy']:.2%}")
        for case in payload["cases"]:
            mark = "✅" if case["passed"] else "❌"
            print(f"  {mark} {case['id']} nodes={case['nodes']} tools={case['tools']}")
            if case["missing_nodes"]:
                print(f"     缺少节点: {case['missing_nodes']}")
    return 0 if report.total and report.accuracy >= 0.99 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="执行轨迹评估")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.json)))


if __name__ == "__main__":
    main()

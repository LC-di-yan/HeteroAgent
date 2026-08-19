"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，根据意图路由到对应子Agent，汇总结果返回。
采用LangGraph StateGraph实现，支持并行调度和Human-in-the-Loop断点。
"""

from __future__ import annotations

import asyncio
import operator
import os
import re
import time
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

from agents.intent_router import IntentRouterAgent
from agents.knowledge_rag import KnowledgeRAGAgent
from agents.ticket_handler import TicketHandlerAgent
from agents.compliance_checker import ComplianceCheckerAgent
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call


# ─── 路由缓存（Q7 — Token 优化）───
# 相同意图的路由决策缓存，避免重复 LLM 调用
_ROUTE_CACHE: dict[str, tuple[str, float]] = {}
_ROUTE_CACHE_TTL = int(os.getenv("ROUTE_CACHE_TTL", "300"))  # 默认 5 分钟

# ─── FAQ 知识库（Q7 — 早退机制）───
# 高频问题精确匹配，命中后跳过 Agent 链路直接返回
FAQ_KNOWLEDGE: dict[str, str] = {
    "理财产品a的收益率": "我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。\n\n以上信息仅供参考，具体以合同条款为准。理财非存款，产品有风险，投资须谨慎。\n\n来源：product_faq.md",
    "退款政策": "退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。\n\n来源：refund_policy.md",
    "如何开户": "开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。\n\n来源：account_guide.md",
    "开户流程": "开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。\n\n来源：account_guide.md",
}


def _normalize_for_cache(text: str) -> str:
    """归一化消息用于缓存 key：去除标点、转小写、取前 50 字符"""
    cleaned = re.sub(r'[^\w\u4e00-\u9fff]', '', text.lower())
    return cleaned[:50]


def _check_faq(message: str) -> str | None:
    """
    FAQ 精确匹配早退（Q7）。
    高频问题直接返回，不走 Agent 链路，节省 Token 和延迟。
    """
    normalized = _normalize_for_cache(message)
    for key, answer in FAQ_KNOWLEDGE.items():
        if key in normalized or normalized in key:
            return answer
    return None


# ─── 超时配置 ───
AGENT_TIMEOUT_SECONDS = 10

# 重试配置（Q8 — 指数退避）
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # 首次重试等待 1s，之后 2s, 4s

_TIMEOUT_FALLBACKS = {
    "knowledge_rag": "知识检索超时，已记录您的问题并将转交人工客服处理，请稍候。",
    "ticket_handler": "工单提交超时，系统正在后台继续处理，请稍后查询。",
    "compliance_check": None,  # 合规超时 → 按不通过处理
}


def _is_retryable(exc: Exception) -> bool:
    """
    判断异常是否可重试（Q8）。
    超时、限流（429）、服务不可用（503/ConnectionError）可重试；
    参数错误、认证失败等不可重试。
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    msg = str(exc).lower()
    retryable_keywords = (
        "timeout", "timed out", "rate limit", "429",
        "503", "unavailable", "connection", "temporarily",
        "server error", "500", "too many requests",
    )
    return any(kw in msg for kw in retryable_keywords)


def _wrap_with_timeout(agent_name: str, agent_func):
    """
    为子Agent节点包装超时熔断 + 指数退避重试（Q8）。

    策略：
    1. 每次尝试限时 AGENT_TIMEOUT_SECONDS
    2. 可重试错误（超时/限流）→ 指数退避重试，最多 MAX_RETRIES 次
    3. 重试耗尽 → 返回降级回复
    """
    async def wrapper(state: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        last_exc: Exception | None = None

        while attempt <= MAX_RETRIES:
            try:
                return await asyncio.wait_for(agent_func(state), timeout=AGENT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as e:
                last_exc = e
                attempt += 1
                if attempt > MAX_RETRIES:
                    break
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))  # 1s, 2s, 4s
                await asyncio.sleep(delay)
            except Exception as e:
                last_exc = e
                if not _is_retryable(e) or attempt >= MAX_RETRIES:
                    break
                attempt += 1
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        # ── 重试耗尽 → 降级回复 ──
        fallback = _TIMEOUT_FALLBACKS.get(agent_name)
        if fallback is None:
            # 合规审查失败/超时 → 按不通过处理
            return {
                **state,
                "compliance_passed": False,
                "sub_results": {
                    **state.get("sub_results", {}),
                    agent_name: f"合规审查异常（{type(last_exc).__name__}），已自动转交人工处理。",
                },
            }
        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                agent_name: fallback,
            },
        }
    return wrapper


# ─── 状态定义 ───

class AgentState(TypedDict):
    """Supervisor编排的全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    intent: str
    sub_results: dict[str, Any]
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int


# ─── Supervisor节点 ───

SUPERVISOR_SYSTEM_PROMPT = """你是一个智能客服系统的Supervisor（主管编排Agent）。
你的职责是：
1. 分析用户意图，决定分发给哪个子Agent处理
2. 汇总子Agent的处理结果，生成最终回复
3. 确保所有回复都经过合规审查（合规审查是自动强制执行的，不需要你路由过去）

可用的子Agent（只返回其中之一）：
- knowledge_rag: 知识库问答 —— 产品咨询、收益率、开户流程、退款政策、风险咨询等
- ticket_handler: 工单处理 —— 退款申请、理赔、开户办理、投诉、查工单状态等业务办理
- compliance_checker: 合规审查 —— 仅在用户询问合规政策、监管要求、投诉举报时使用

路由规则：
- 用户咨询产品信息、问收益/风险/政策 → knowledge_rag
- 用户要办理业务（退款/开户/理赔/投诉）或查工单 → ticket_handler
- 用户只是问"能不能保证收益/稳赚不赔"这类问题 → knowledge_rag（知识库会给出合规回答，不要路由到 compliance_checker）

根据用户消息，返回路由目标。
"""


class SupervisorNode:
    """Supervisor决策节点"""

    def __init__(self, llm: ChatOpenAI, working_memory: WorkingMemory):
        self.llm = llm
        self.working_memory = working_memory

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> AgentState:
        """
        分析用户意图，决定路由。

        优化策略（Q7）：
        1. FAQ 早退 — 高频问题精确匹配，跳过 Agent 链路直接返回
        2. 路由缓存 — 相同意图缓存，避免重复 LLM 调用
        3. LLM 路由 — 兜底使用 LLM 判断意图
        """
        messages = state["messages"]
        session_id = state.get("session_id", "default")
        user_message = messages[-1].content if messages else ""

        # ── 策略1: FAQ 早退（节省 ~2s LLM 调用）──
        faq_answer = _check_faq(user_message)
        if faq_answer:
            return {
                **state,
                "intent": "compliance_checker",
                "current_agent": "supervisor",
                "sub_results": {
                    **state.get("sub_results", {}),
                    "faq_direct": faq_answer,
                },
            }

        # ── 策略2: 路由缓存（避免重复 LLM 调用）──
        cache_key = _normalize_for_cache(user_message)
        now = time.time()

        if cache_key in _ROUTE_CACHE:
            cached_intent, cached_at = _ROUTE_CACHE[cache_key]
            if now - cached_at < _ROUTE_CACHE_TTL:
                self.working_memory.update(session_id, {"last_intent": cached_intent})
                return {
                    **state,
                    "intent": cached_intent,
                    "current_agent": "supervisor",
                }
            else:
                del _ROUTE_CACHE[cache_key]

        # ── 策略3: LLM 路由（兜底）──
        context = self.working_memory.get_context(session_id)

        routing_prompt = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            SystemMessage(content=f"当前工作记忆上下文: {context}"),
            *messages,
            HumanMessage(content=(
                "请分析用户的最新消息，返回应该路由到的Agent名称。"
                "只返回以下之一: knowledge_rag, ticket_handler, compliance_checker"
            )),
        ]

        response = await self.llm.ainvoke(routing_prompt)
        intent = response.content.strip().lower()

        valid_intents = {"knowledge_rag", "ticket_handler", "compliance_checker"}
        if intent not in valid_intents:
            intent = "knowledge_rag"

        # 写入缓存
        _ROUTE_CACHE[cache_key] = (intent, now)

        self.working_memory.update(session_id, {"last_intent": intent})

        return {
            **state,
            "intent": intent,
            "current_agent": "supervisor",
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> AgentState:
        """汇总子Agent结果，生成最终回复"""
        sub_results = state.get("sub_results", {})
        compliance_passed = state.get("compliance_passed", True)

        if not compliance_passed:
            final_response = (
                "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
                "工单编号已自动生成，请留意后续通知。"
            )
        else:
            result_parts = []
            for agent_name, result in sub_results.items():
                # 只拼接字符串类型的子结果（跳过 compliance dict 等）
                if isinstance(result, str) and result.strip():
                    result_parts.append(result)
            final_response = "\n\n".join(result_parts) if result_parts else "抱歉，暂时无法处理您的请求，请稍后重试。"

        return {
            **state,
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)],
        }


# ─── 路由函数 ───

def route_to_agent(state: AgentState) -> str:
    """根据意图路由到对应Agent节点"""
    intent = state.get("intent", "knowledge_rag")
    route_map = {
        "knowledge_rag": "knowledge_rag",
        "ticket_handler": "ticket_handler",
        "compliance_checker": "compliance_check",
    }
    return route_map.get(intent, "knowledge_rag")


def should_check_compliance(state: AgentState) -> str:
    """所有回复都需经过合规审查"""
    return "compliance_check"


# ─── 构建Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    short_term_memory: ShortTermMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    enable_checkpointing: bool = True,
) -> StateGraph:
    """
    构建Supervisor编排的多Agent StateGraph。

    这是整个系统的核心入口，将4个子Agent通过有向图连接起来，
    由Supervisor节点负责路由决策和结果汇总。

    Args:
        llm: 语言模型实例
        working_memory: 工作记忆
        short_term_memory: 短期记忆
        long_term_memory: 长期记忆
        enable_checkpointing: 是否启用检查点（支持断点恢复）
    """
    if llm is None:
        import os
        llm = ChatOpenAI(
            model=os.getenv("MODEL_NAME", "gpt-4o"),
            temperature=0,
        )
    if working_memory is None:
        working_memory = WorkingMemory()

    supervisor = SupervisorNode(llm, working_memory)

    intent_router = IntentRouterAgent(llm)
    knowledge_agent = KnowledgeRAGAgent(llm, long_term_memory)
    ticket_agent = TicketHandlerAgent(llm)
    compliance_agent = ComplianceCheckerAgent(llm)

    graph = StateGraph(AgentState)

    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("knowledge_rag", _wrap_with_timeout("knowledge_rag", knowledge_agent.process))
    graph.add_node("ticket_handler", _wrap_with_timeout("ticket_handler", ticket_agent.process))
    graph.add_node("compliance_check", _wrap_with_timeout("compliance_check", compliance_agent.process))
    graph.add_node("synthesize", supervisor.synthesize_response)

    graph.set_entry_point("supervisor_route")

    graph.add_conditional_edges(
        "supervisor_route",
        route_to_agent,
        {
            "knowledge_rag": "knowledge_rag",
            "ticket_handler": "ticket_handler",
            "compliance_check": "compliance_check",
        },
    )

    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled

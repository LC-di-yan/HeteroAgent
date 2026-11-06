"""Supervisor 编排 —— P0 清理版。

相对原实现的变化（T-P0-02/04/10 + 4.1.1）：
- 删除 FAQ 早退与进程级路由缓存（跨用户共享、无上限、写错 intent）；
- 路由解析改为容错解析 + 白名单，compliance 意图改走知识库（原实现返回空回复）；
- 重试按异常类型判定，不再匹配英文错误字符串；
- 聚合不再编造工单号（G10）；
- 超时/重试参数来自集中配置。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from agents.llm_utils import run_structured
from agents.schemas import RouteDecision
from config import Settings
from config import settings as default_settings
from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)

try:  # 按异常类型判定可重试，避免匹配错误消息文本
    from openai import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    _RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)
except Exception:  # pragma: no cover - openai 不可用时退化为仅超时可重试
    _RETRYABLE = ()


# ─── 状态 ───

class AgentState(TypedDict):
    """Supervisor 编排的全局状态。"""

    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    trace_id: str
    intent: str
    sub_results: dict[str, Any]
    compliance_passed: bool
    final_response: str
    retry_count: int
    approval_id: str | None
    approval_decision: dict[str, Any] | None


# ─── 路由解析 ───

ROUTE_KNOWLEDGE = "knowledge_rag"
ROUTE_TICKET = "ticket_handler"
# compliance 类意图改走知识库：原实现直接跳合规节点，没有任何业务 Agent 产出，用户会收到空回复
_ROUTE_ALIASES = {
    "ticket_handler": ROUTE_TICKET,
    "knowledge_rag": ROUTE_KNOWLEDGE,
    "compliance_checker": ROUTE_KNOWLEDGE,
    "compliance_check": ROUTE_KNOWLEDGE,
    "compliance": ROUTE_KNOWLEDGE,
}
_VALID_ROUTES = {ROUTE_KNOWLEDGE, ROUTE_TICKET}


def parse_route(text: str) -> str:
    """从模型自由文本中提取路由目标；无法识别时兜底 knowledge_rag。"""
    cleaned = (text or "").strip().lower().replace("```", " ")
    # 先匹配更具体的 ticket_handler，再匹配 knowledge_rag，最后 compliance*
    for token in ("ticket_handler", "knowledge_rag", "compliance_checker", "compliance"):
        if token in cleaned:
            return _ROUTE_ALIASES.get(token, ROUTE_KNOWLEDGE)
    return ROUTE_KNOWLEDGE


SUPERVISOR_SYSTEM_PROMPT = """你是一个智能客服系统的 Supervisor（主管编排 Agent）。

可用的子 Agent（只返回其中之一，不要输出任何其他文字）：
- knowledge_rag: 知识库问答 —— 产品咨询、收益率、开户/退款政策、风险说明、合规政策咨询
- ticket_handler: 工单处理 —— 退款申请、理赔、开户办理、投诉登记、查询工单进度

路由规则：
- 咨询信息、问政策/收益/风险 → knowledge_rag
- 要办理业务（退款/开户/理赔/投诉）或查工单 → ticket_handler
- 涉及合规政策、监管要求 → knowledge_rag

只输出一个单词：knowledge_rag 或 ticket_handler
"""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if _RETRYABLE and isinstance(exc, _RETRYABLE):
        return True
    return isinstance(exc, ConnectionError)


_TIMEOUT_FALLBACKS = {
    ROUTE_KNOWLEDGE: "知识检索超时，请稍后重试，或直接联系人工客服。",
    ROUTE_TICKET: "工单服务繁忙，请稍后重试；如已提交，可用工单号查询进度。",
}


def _wrap_with_timeout(agent_name: str, agent_func, settings: Settings = default_settings):
    """为子 Agent 节点加超时与有界重试（指数退避 + 抖动）。"""

    async def wrapper(state: dict[str, Any]) -> dict[str, Any]:
        timeout = float(settings.llm_timeout_seconds)
        max_retries = int(settings.llm_max_retries)
        attempt = 0
        last_exc: BaseException | None = None

        while attempt <= max_retries:
            try:
                return await asyncio.wait_for(agent_func(state), timeout=timeout)
            except BaseException as exc:  # noqa: BLE001 - 需要统一归类
                last_exc = exc
                if not _is_retryable(exc) or attempt >= max_retries:
                    break
                attempt += 1
                delay = min(8.0, 0.5 * (2 ** (attempt - 1)))
                logger.warning("agent %s 第 %d 次重试：%s", agent_name, attempt, type(exc).__name__)
                await asyncio.sleep(delay)

        fallback = _TIMEOUT_FALLBACKS.get(agent_name)
        if fallback is None:
            # 合规节点失败 → 按不通过处理（fail-closed）
            return {
                **state,
                "compliance_passed": False,
                "sub_results": {
                    **state.get("sub_results", {}),
                    agent_name: {"passed": False, "risk_level": "high",
                                 "violations": [f"节点异常：{type(last_exc).__name__}"]},
                },
            }
        return {
            **state,
            "sub_results": {**state.get("sub_results", {}), agent_name: fallback},
        }

    return wrapper


class SupervisorNode:
    """路由决策、HITL 闸门与结果聚合。"""

    def __init__(self, llm: Any, settings: Settings = default_settings, approval_store: Any | None = None):
        self.llm = llm
        self.settings = settings
        self.approval_store = approval_store

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> AgentState:
        messages = state.get("messages") or []
        if not messages:
            return {**state, "intent": ROUTE_KNOWLEDGE}

        routing_prompt = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            *messages[-6:],
            HumanMessage(content="只返回 knowledge_rag 或 ticket_handler。"),
        ]
        # 首选结构化输出（T-P1-03）：target 由 schema 限定，避免自由文本歧义
        decision = await run_structured(self.llm, RouteDecision, routing_prompt, label="route")
        if decision is not None:
            return {**state, "intent": _ROUTE_ALIASES.get(decision.target, ROUTE_KNOWLEDGE)}

        response = await self.llm.ainvoke(routing_prompt)
        content = getattr(response, "content", "") or ""
        intent = parse_route(content)
        if intent not in _VALID_ROUTES:  # 防御性兜底
            intent = ROUTE_KNOWLEDGE
        return {**state, "intent": intent}

    @trace_agent_call("compliance_gate")
    async def compliance_gate(self, state: AgentState) -> AgentState:
        """HITL 闸门（T-P1-05）：合规未通过时在图内 interrupt()，等待人工审批后恢复。

        只有在注入了 approval_store 且图带 checkpointer 时才会真正中断；
        否则原样放行，由 API 层的外部审批队列兜底（保持向后兼容）。
        """
        from langgraph.types import interrupt

        if state.get("compliance_passed", True):
            return state
        if self.approval_store is None:
            return state

        sub_results = state.get("sub_results", {})
        info = sub_results.get("compliance") if isinstance(sub_results.get("compliance"), dict) else {}
        violations = list(info.get("violations", []) or [])
        risk_level = str(info.get("risk_level", "high"))
        agent_responses = {
            key: value for key, value in sub_results.items()
            if key != "compliance" and isinstance(value, str) and value.strip()
        }
        messages = state.get("messages") or []
        user_message = getattr(messages[-1], "content", "") if messages else ""

        record = await self.approval_store.new_request(
            tenant_id=state.get("user_id", "anonymous"),
            session_id=state.get("session_id", ""),
            trace_id=state.get("trace_id", ""),
            user_message=user_message,
            agent_responses=agent_responses,
            violations=violations,
            risk_level=risk_level,
            intent=str(state.get("intent", "unknown")),
        )
        decision = interrupt({
            "approval_id": record.approval_id,
            "reason": "compliance",
            "violations": violations,
            "risk_level": risk_level,
        })
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else False
        logger.info("HITL 决策 approval_id=%s approved=%s", record.approval_id, approved)
        return {
            **state,
            "approval_id": record.approval_id,
            "approval_decision": {
                "approved": approved,
                "actor": str(decision.get("actor", "")) if isinstance(decision, dict) else "",
                "reason": str(decision.get("reason", "")) if isinstance(decision, dict) else "",
            },
            "compliance_passed": approved,
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> AgentState:
        """聚合子结果。合规未通过时只给中性转人工提示，不编造任何单据编号。"""
        sub_results = state.get("sub_results", {})
        compliance_passed = state.get("compliance_passed", True)
        decision = state.get("approval_decision") or {}
        parts = [
            value.strip()
            for key, value in sub_results.items()
            if key != "compliance" and isinstance(value, str) and value.strip()
        ]

        if decision.get("approved") is True:
            # 人工放行：先清洗违规用语/PII，绝不放回原文（G3）
            from agents.compliance_checker import sanitize_output

            body = "\n\n".join(sanitize_output(part) for part in parts)
            final_response = (
                f"✅ [人工审批通过] {body}" if body
                else "您的请求已由人工审核通过，客服人员将尽快处理。"
            )
        elif decision and decision.get("approved") is False:
            # 已有人工结论且为拒绝：明确告知未通过，不再说"转入审核中"
            final_response = "❌ 您的请求经人工审核未通过。如有疑问，请联系人工客服。"
        elif not compliance_passed:
            final_response = (
                "您的请求涉及敏感内容或需要人工确认，已转入人工审核流程。"
                "客服人员核实后会尽快与您联系。"
            )
        else:
            final_response = (
                "\n\n".join(parts)
                if parts
                else "抱歉，暂时无法处理您的请求，请稍后重试或联系人工客服。"
            )

        return {
            **state,
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)],
        }


def route_to_agent(state: AgentState) -> str:
    intent = state.get("intent", ROUTE_KNOWLEDGE)
    return ROUTE_TICKET if intent == ROUTE_TICKET else ROUTE_KNOWLEDGE


def _build_checkpointer(settings: Settings):
    if settings.checkpoint_backend == "postgres":
        dsn = (settings.postgres_dsn or "").strip()
        if not dsn:
            raise RuntimeError(
                "CHECKPOINT_BACKEND=postgres 但 POSTGRES_DSN 为空；"
                "请设置 POSTGRES_DSN 或改用 sqlite"
            )
        try:
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:  # 可选依赖
            raise RuntimeError(
                "缺少 langgraph-checkpoint-postgres，请执行 "
                "pip install langgraph-checkpoint-postgres 后再启用 Postgres 检查点"
            ) from exc
        try:
            # 用长连接构造（from_conn_string 是上下文管理器，不能直接交给 compile）
            conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=0)
            saver = PostgresSaver(conn)
            saver.setup()  # 首次运行时建检查点表；已存在则幂等
        except Exception as exc:
            raise RuntimeError(f"Postgres 检查点初始化失败: {exc}") from exc
        logger.info("检查点后端: Postgres（多副本共享）")
        return saver

    if settings.checkpoint_backend == "sqlite":
        try:
            import os
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver

            os.makedirs(os.path.dirname(settings.checkpoint_path) or ".", exist_ok=True)
            conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
            logger.info("检查点后端: SQLite(%s)", settings.checkpoint_path)
            return SqliteSaver(conn)
        except Exception as exc:  # pragma: no cover
            logger.warning("SQLite 检查点不可用，回退内存: %s", exc)
    return MemorySaver()


def create_supervisor_graph(
    llm: Any | None = None,
    working_memory: Any | None = None,   # 已废弃：保留参数仅为兼容旧调用
    short_term_memory: Any | None = None,  # 同上
    long_term_memory: Any | None = None,
    enable_checkpointing: bool = True,
    settings: Settings = default_settings,
    approval_store: Any | None = None,
):
    """构建 Supervisor 编排图（5 节点语义 + HITL 闸门）。

    approval_store 与 checkpointer 同时存在时，合规失败会在图内 interrupt()，
    审批通过后由 Command(resume=...) 恢复执行（T-P1-05）。
    """
    from agents.compliance_checker import ComplianceCheckerAgent
    from agents.knowledge_rag import KnowledgeRAGAgent
    from agents.ticket_handler import TicketHandlerAgent

    if llm is None:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=settings.model_name,
            temperature=settings.temperature,
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )

    checkpointer = _build_checkpointer(settings) if enable_checkpointing else None
    # 没有 checkpointer 就无法中断/恢复，此时不注入 store，走 API 层外部审批兜底
    hitl_store = approval_store if checkpointer is not None else None

    supervisor = SupervisorNode(llm, settings, approval_store=hitl_store)
    knowledge_agent = KnowledgeRAGAgent(llm, long_term_memory, settings=settings)
    tools = None
    if getattr(settings, "tool_calling_enabled", True):
        try:
            from agents.toolset import build_tools

            tools = build_tools()
        except Exception as exc:  # 工具集不可用不影响主链路
            logger.warning("工具集加载失败: %s", exc)
    ticket_agent = TicketHandlerAgent(llm, tools=tools)
    compliance_agent = ComplianceCheckerAgent(llm)

    graph = StateGraph(AgentState)
    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("knowledge_rag", _wrap_with_timeout(ROUTE_KNOWLEDGE, knowledge_agent.process, settings))
    graph.add_node("ticket_handler", _wrap_with_timeout(ROUTE_TICKET, ticket_agent.process, settings))
    graph.add_node("compliance_check", _wrap_with_timeout("compliance_check", compliance_agent.process, settings))
    graph.add_node("compliance_gate", supervisor.compliance_gate)
    graph.add_node("synthesize", supervisor.synthesize_response)

    graph.set_entry_point("supervisor_route")
    graph.add_conditional_edges(
        "supervisor_route",
        route_to_agent,
        {ROUTE_KNOWLEDGE: ROUTE_KNOWLEDGE, ROUTE_TICKET: ROUTE_TICKET},
    )
    graph.add_edge(ROUTE_KNOWLEDGE, "compliance_check")
    graph.add_edge(ROUTE_TICKET, "compliance_check")
    graph.add_edge("compliance_check", "compliance_gate")
    graph.add_edge("compliance_gate", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile(checkpointer=checkpointer)

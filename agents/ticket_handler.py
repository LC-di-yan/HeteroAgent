"""
工单处理Agent — 工单CRUD与流转
负责创建、查询、更新工单，对接工单系统，处理退款/理赔/开户等业务办理类需求。
通过MCP工具协议调用外部工单系统。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agents.llm_utils import run_structured
from agents.schemas import TicketAction
from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)


class TicketStatus(StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ESCALATED = "escalated"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


TICKET_TOOL_PROMPT = """你是工单处理助手。根据用户诉求决定是否调用工具：

- 用户要办理退款/理赔/开户/投诉等业务 → 调用 ticket_create 创建工单
- 用户提供订单号或问订单情况 → 调用 order_query
- 涉及金额较大或资金安全 → 先调用 risk_check 再建单
- 只是咨询、无需建单 → 直接回答，不要调用工具

创建工单后，用中文回复并带上真实工单号，不要编造工单号。"""


TICKET_SYSTEM_PROMPT = """你是一个专业的工单处理Agent，负责处理客户的业务办理请求。

你的职责：
1. 分析用户需求，判断是否需要创建工单
2. 提取工单关键信息（类型、优先级、描述）
3. 创建工单并返回工单号
4. 查询现有工单状态

工单类型：
- refund: 退款申请
- claim: 理赔申请
- account_open: 开户申请
- account_change: 账户变更
- complaint: 投诉工单
- general: 通用工单

优先级判断规则：
- urgent: 资金安全、账户被盗
- high: 退款超时、理赔争议
- medium: 常规业务办理
- low: 信息咨询类

请以JSON格式返回工单信息：
{
    "action": "create|query|update",
    "ticket_type": "refund|claim|account_open|...",
    "priority": "low|medium|high|urgent",
    "summary": "工单摘要",
    "details": "详细描述"
}
"""


class TicketStore:
    """工单存储适配器。

    传入 repo（TicketRepository）时委托给共享仓储——与 MCP 工具路径共用同一实例，
    偿还 ADR-0007 的双存储分裂。未传入时保持旧的进程内行为（测试/演示兼容）。
    """

    def __init__(self, repo: Any | None = None):
        self._repo = repo
        self._tickets: dict[str, dict] = {}

    def create(self, ticket_type: str, priority: str, summary: str, details: str, user_id: str) -> dict:
        if self._repo is not None:
            return self._repo.create(
                ticket_type=ticket_type, priority=priority,
                summary=summary, details=details, user_id=user_id,
            )
        ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": TicketStatus.CREATED.value,
            "summary": summary,
            "details": details,
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        self._tickets[ticket_id] = ticket
        return ticket

    def query(self, ticket_id: str) -> dict | None:
        if self._repo is not None:
            return self._repo.get(ticket_id)
        return self._tickets.get(ticket_id)

    def query_by_user(self, user_id: str) -> list[dict]:
        if self._repo is not None:
            return self._repo.query_by_user(user_id)
        return [t for t in self._tickets.values() if t["user_id"] == user_id]

    def update_status(self, ticket_id: str, status: str) -> dict | None:
        if self._repo is not None:
            return self._repo.update_status(ticket_id, status)
        ticket = self._tickets.get(ticket_id)
        if ticket:
            ticket["status"] = status
            ticket["updated_at"] = datetime.now().isoformat()
        return ticket


class TicketHandlerAgent:
    """工单处理Agent"""

    _TICKET_RE = re.compile(r"TK-\d{8}-[A-F0-9]{6}")

    def __init__(self, llm: ChatOpenAI, ticket_store: TicketStore | None = None,
                 tools: list[Any] | None = None, enable_tool_calling: bool = True,
                 context_store: Any | None = None):
        self.llm = llm
        self.ticket_store = ticket_store or TicketStore()
        self.context_store = context_store
        self._tool_agent = None
        if enable_tool_calling and tools:
            # T-P1-07：模型支持 tool calling 时，工单动作交给 create_agent 的工具循环，
            # 不再依赖"LLM 写 JSON → 我们解析"这条脆弱链路。
            try:
                from agents.react_agent import build_tool_agent

                self._tool_agent = build_tool_agent(
                    llm, tools, TICKET_TOOL_PROMPT, "ticket_agent"
                )
                logger.info("工单 Agent 已启用工具调用（%d 个工具）", len(tools))
            except Exception as exc:
                logger.info("工具调用不可用，回退文本解析: %s", exc)
                self._tool_agent = None

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str) -> dict | None:
        """
        分析用户需求，提取工单信息。

        先做规则预判（正则识别工单号 → 查询），再让 LLM 兜底分析。
        解析失败返回 None——绝不把「解析失败」当成「创建工单」的默认动作
        （原实现会在模型输出畸形时静默创建垃圾工单）。
        """
        import json
        import re

        # ── 规则预判：包含工单号 → 查询操作 ──
        ticket_id_match = re.search(r"(TK-\d{8}-[A-F0-9]{6}|TK-\d{8}-\d{6})", user_message.upper())
        if ticket_id_match:
            return {
                "action": "query",
                "ticket_id": ticket_id_match.group(0),
                "ticket_type": "general",
                "priority": "low",
                "summary": f"查询工单 {ticket_id_match.group(0)} 状态",
                "details": user_message,
            }

        messages = [
            SystemMessage(content=TICKET_SYSTEM_PROMPT),
            HumanMessage(content=f"用户消息: {user_message}"),
        ]

        # 首选结构化输出（T-P1-03）：schema 保证字段合法；失败返回 None 而非默认建单
        action = await run_structured(self.llm, TicketAction, messages, label="ticket")
        if action is not None:
            return action.model_dump()

        response = await self.llm.ainvoke(messages)
        raw = (getattr(response, "content", "") or "").strip()
        fence = chr(96) * 3
        if raw.startswith(fence):
            raw = raw[len(fence):]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
            if raw.endswith(fence):
                raw = raw[: -len(fence)].strip()

        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return None
            return parsed
        except (json.JSONDecodeError, ValueError):
            logger.warning("工单信息解析失败，放弃自动建单（避免副作用）")
            return None

    @trace_agent_call("ticket_create")
    async def create_ticket(self, ticket_info: dict, user_id: str) -> str:
        """创建工单"""
        ticket = self.ticket_store.create(
            ticket_type=ticket_info.get("ticket_type", "general"),
            priority=ticket_info.get("priority", "medium"),
            summary=ticket_info.get("summary", ""),
            details=ticket_info.get("details", ""),
            user_id=user_id,
        )

        priority_label = {
            "low": "普通", "medium": "中等", "high": "高", "urgent": "紧急"
        }.get(ticket["priority"], "中等")

        return (
            f"工单已创建成功！\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📝 类型: {ticket['type']}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n\n"
            f"我们将尽快处理您的请求，请保存好工单号以便后续查询。"
        )

    def _remember_ticket(self, user_id: str, text: str) -> None:
        """把工单号写入用户级事实（T-P2-02），供后续会话直接引用。"""
        if self.context_store is None:
            return
        match = self._TICKET_RE.search(text or "")
        if not match:
            return
        try:
            self.context_store.put_fact(user_id, "ticket", match.group(0))
        except Exception as exc:  # 记忆写入失败不影响主流程
            logger.warning("用户事实写入失败: %s", type(exc).__name__)

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str) -> str:
        """查询工单状态"""
        ticket = self.ticket_store.query(ticket_id)
        if not ticket:
            return f"未找到工单号 {ticket_id}，请确认工单号是否正确。"

        status_label = {
            "created": "已创建",
            "processing": "处理中",
            "pending_review": "待审核",
            "resolved": "已解决",
            "closed": "已关闭",
            "escalated": "已升级",
        }.get(ticket["status"], ticket["status"])

        return (
            f"工单查询结果：\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📊 状态: {status_label}\n"
            f"📝 类型: {ticket['type']}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n"
            f"🔄 更新时间: {ticket['updated_at']}"
        )

    @trace_agent_call("ticket_tool_call")
    async def _process_with_tools(self, user_message: str) -> str | None:
        """走 create_agent 的工具循环；失败返回 None 由调用方回退文本解析。"""
        try:
            out = await self._tool_agent.ainvoke(
                {"messages": [HumanMessage(content=user_message)]}
            )
        except Exception as exc:
            logger.warning("工具调用失败，回退文本解析: %s", type(exc).__name__)
            return None
        messages = out.get("messages", []) if isinstance(out, dict) else []
        for msg in reversed(messages):
            content = getattr(msg, "content", "")
            if msg.__class__.__name__ == "AIMessage" and isinstance(content, str) and content.strip():
                return content.strip()
        return None

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        user_id = state.get("user_id", "anonymous")

        if not messages:
            return state

        last_message = messages[-1].content

        # 工单号查询走规则快路径（无需 LLM / 工具）
        import re as _re

        if _re.search(r"TK-\d{8}-[A-F0-9]{6}", last_message.upper()):
            ticket_info = await self.analyze_request(last_message)
            result = (
                await self.query_ticket(ticket_info["ticket_id"])
                if ticket_info and ticket_info.get("ticket_id")
                else "未找到对应工单，请核对工单号。"
            )
            return {**state, "sub_results": {**state.get("sub_results", {}), "ticket_handler": result}}

        # 工具调用路径（T-P1-07）：动作由 tool_calls 驱动，而非解析模型文本
        if self._tool_agent is not None:
            tool_answer = await self._process_with_tools(last_message)
            if tool_answer:
                self._remember_ticket(user_id, tool_answer)
                return {**state, "sub_results": {
                    **state.get("sub_results", {}), "ticket_handler": tool_answer}}

        ticket_info = await self.analyze_request(last_message)

        if ticket_info is None:
            result = ("抱歉，我没能完整理解您的需求。请说明您要办理的业务"
                      "（例如退款、理赔、开户），或提供工单号以便查询。")
        else:
            action = ticket_info.get("action", "create")
            if action == "query" and ticket_info.get("ticket_id"):
                result = await self.query_ticket(ticket_info["ticket_id"])
            else:
                result = await self.create_ticket(ticket_info, user_id)
                self._remember_ticket(user_id, result)

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": result,
            },
        }

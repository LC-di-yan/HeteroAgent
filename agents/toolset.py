"""把 MCP 工具适配为 LangChain 工具（T-P1-07）。

- 工具实现统一走 tooling.client（进程内直调官方 MCP SDK 注册的函数）；
- 每次调用包一层 execute_tool span（gen_ai.tool.name / 参数字节数，不记原文）；
- 返回值为 JSON 字符串，便于模型阅读，也便于测试断言。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tracing.otel_config import tool_span

logger = logging.getLogger(__name__)


# ── 参数 schema（同时作为给模型的 tool 定义）──

class OrderQueryArgs(BaseModel):
    order_id: str = Field(default="", description="订单号")
    user_id: str = Field(default="", description="用户 ID")


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索关键词")
    top_k: int = Field(default=3, ge=1, le=20, description="返回条数")


class TicketCreateArgs(BaseModel):
    title: str = Field(description="工单标题")
    description: str = Field(description="问题详情")
    priority: str = Field(default="medium", description="low/medium/high/urgent")
    category: str = Field(default="general", description="工单分类")
    idempotency_key: str | None = Field(default=None, description="幂等键，避免重复建单")


class RiskCheckArgs(BaseModel):
    user_id: str = Field(description="用户 ID")
    action: str = Field(description="操作类型，如 refund/transfer")
    amount: float = Field(default=0.0, ge=0, description="金额")


async def _invoke(name: str, arguments: dict[str, Any]) -> str:
    """调用 MCP 工具并产出 execute_tool span。"""
    payload = json.dumps(arguments, ensure_ascii=False)
    with tool_span(name, arg_size=len(payload.encode("utf-8"))):
        from tooling.client import call_tool

        result = await call_tool(name, arguments)
    return json.dumps(result, ensure_ascii=False)


async def order_query(order_id: str = "", user_id: str = "") -> str:
    return await _invoke("order_query", {"order_id": order_id, "user_id": user_id})


async def knowledge_search(query: str, top_k: int = 3) -> str:
    return await _invoke("knowledge_search", {"query": query, "top_k": top_k})


async def ticket_create(
    title: str,
    description: str,
    priority: str = "medium",
    category: str = "general",
    idempotency_key: str | None = None,
) -> str:
    args: dict[str, Any] = {
        "title": title,
        "description": description,
        "priority": priority,
        "category": category,
    }
    if idempotency_key:
        args["idempotency_key"] = idempotency_key
    return await _invoke("ticket_create", args)


async def risk_check(user_id: str, action: str, amount: float = 0.0) -> str:
    return await _invoke("risk_check", {"user_id": user_id, "action": action, "amount": amount})


def build_tools() -> list[StructuredTool]:
    """构建可绑定给 create_agent 的工具列表。"""
    return [
        StructuredTool.from_function(
            coroutine=order_query,
            name="order_query",
            description="查询订单信息（按订单号或用户 ID）。",
            args_schema=OrderQueryArgs,
        ),
        StructuredTool.from_function(
            coroutine=knowledge_search,
            name="knowledge_search",
            description="检索企业知识库，返回相关文档片段与来源。",
            args_schema=KnowledgeSearchArgs,
        ),
        StructuredTool.from_function(
            coroutine=ticket_create,
            name="ticket_create",
            description="创建客服工单并返回工单号（TK-YYYYMMDD-XXXXXX）。",
            args_schema=TicketCreateArgs,
        ),
        StructuredTool.from_function(
            coroutine=risk_check,
            name="risk_check",
            description="风控检查：给出操作的风险等级与是否需要人工复核。",
            args_schema=RiskCheckArgs,
        ),
    ]


__all__ = ["build_tools", "order_query", "knowledge_search", "ticket_create", "risk_check"]

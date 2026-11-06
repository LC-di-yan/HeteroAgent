"""结构化输出契约（T-P1-03）。

模型输出不再靠"文本里找关键词"或"失败就兜底造动作"：
每个有副作用的决策都由 Pydantic schema 约束，解析失败显式返回 None 由调用方决定降级语义。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RouteDecision(BaseModel):
    """Supervisor 路由决策。"""

    target: Literal["knowledge_rag", "ticket_handler", "compliance_checker"] = Field(
        default="knowledge_rag", description="分发到的子 Agent"
    )
    intent_category: Literal[
        "consultation", "complaint", "transaction", "account", "compliance", "unknown"
    ] = "unknown"
    secondary_intent: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    entities: dict[str, str] = Field(default_factory=dict)


class TicketAction(BaseModel):
    """工单动作。解析失败时必须返回 None，不得默认 create。"""

    action: Literal["create", "query", "update"] = "create"
    ticket_id: str | None = None
    ticket_type: Literal[
        "refund", "claim", "account_open", "account_change", "complaint", "general"
    ] = "general"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    summary: str = ""
    details: str = ""


class ComplianceVerdict(BaseModel):
    """合规审查结论。解析失败 = fail-closed，调用方按不通过处理。"""

    passed: bool = False
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    violations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class RerankDecision(BaseModel):
    """重排结果（索引列表，越界由调用方过滤）。"""

    ranked_indices: list[int] = Field(default_factory=list)


class CorrectionVerdict(BaseModel):
    """答案自检结论：passed=True 表示无需修改。"""

    passed: bool = True
    corrected_answer: str = ""


__all__ = [
    "RouteDecision",
    "TicketAction",
    "ComplianceVerdict",
    "RerankDecision",
    "CorrectionVerdict",
]

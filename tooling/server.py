"""HeteroAgent 工具 MCP 服务端 — 基于官方 MCP SDK（缺陷 G12 重构）。

此前本仓库在 mcp/ 目录自造了一套 JSON-RPC MCP 实现，与 PyPI 的 mcp 包互相遮蔽；
现改为官方 SDK（mcp.server.fastmcp.FastMCP / mcp 2.x 的 mcp.server.mcpserver.MCPServer），
并整体迁入 tooling/ 包，彻底消除 import 遮蔽。

本模块提供：
- 进程内 MCP 服务实例（name="heteroagent-tools"），含使用说明 instructions；
- 4 个演示工具：order_query / knowledge_search / ticket_create / risk_check；
- TOOL_FUNCTIONS：工具名 → 原生协程函数的映射，供 tooling.client 进程内直调；
- get_mcp()：返回 MCP 服务实例（tests 与 api 层共用）。

工具通过 @mcp.tool() 注册：签名类型注解 + docstring 会由 SDK 自动生成
JSON Schema（inputSchema），无需手写 schema。
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from typing import Any, Literal

# ─── 官方 MCP SDK 兼容导入（1.x: FastMCP；2.x: 更名为 MCPServer） ───
try:  # mcp SDK 1.x（含 1.27.2）
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp SDK 2.x：FastMCP 更名为 MCPServer，API 同构
    from mcp.server.mcpserver import MCPServer as FastMCP

MCP_SERVER_NAME = "heteroagent-tools"

MCP_INSTRUCTIONS = (
    "HeteroAgent 智能客服演示工具集。通过 MCP tools/call 提供四类能力："
    "订单查询（order_query）、知识库检索（knowledge_search）、工单创建（ticket_create）、"
    "风控校验（risk_check）。所有工具均为内存演示实现，无外部依赖，可离线运行。"
    "调用失败时客户端侧由 tooling.breaker.CircuitBreaker 熔断保护（连续失败 3 次 / 冷却 60 秒）。"
)

# 进程内共享的单例 MCP 服务（FastMCP / MCPServer 均为纯内存对象，可复用）
mcp = FastMCP(
    name=MCP_SERVER_NAME,
    instructions=MCP_INSTRUCTIONS,
)


# ══════════════════════════════════════════════════════════════════
# 演示数据（内存态，离线可用）
# ══════════════════════════════════════════════════════════════════

DEMO_ORDERS: list[dict] = [
    {
        "order_id": "ORD-20260401-001",
        "user_id": "U1001",
        "status": "shipped",
        "amount": 299.00,
        "product": "无线蓝牙耳机",
        "created_at": "2026-04-01T10:00:00",
    },
    {
        "order_id": "ORD-20260402-007",
        "user_id": "U1002",
        "status": "pending_payment",
        "amount": 4599.00,
        "product": "智能电视 65英寸",
        "created_at": "2026-04-02T15:30:00",
    },
]

# 模块级内存知识库：3 条（产品收益 / 退款政策 / 开户流程）
KNOWLEDGE_BASE: list[dict] = [
    {
        "content": "理财产品A（90天封闭期）业绩比较基准为年化3.5%-5.2%，风险等级R2，1万元起购，到期自动赎回至绑定银行卡。",
        "source": "products/wealth_a.md",
    },
    {
        "content": "订单支持7天无理由退款：签收后7天内可申请，退款将在审核通过后3-5个工作日内原路退回支付账户。",
        "source": "policy/refund.md",
    },
    {
        "content": "开户流程：填写身份信息并上传身份证正反面、绑定银行卡、完成人脸核验与风险测评、设置交易密码，全程约5分钟。",
        "source": "guides/account_opening.md",
    },
]

# 幂等工单存储：idempotency_key → 工单结果
_TICKETS_BY_KEY: dict[str, dict] = {}
_TICKETS_BY_ID: dict[str, dict] = {}
_TICKET_LOCK = threading.Lock()

_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    """极简分词：英文按词，中文按 2-gram（演示级检索）。"""
    tokens = [w.lower() for w in _ASCII_RE.findall(text)]
    for seg in _HAN_RE.findall(text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


# ══════════════════════════════════════════════════════════════════
# MCP 工具注册（签名注解 + docstring → 自动 inputSchema）
# ══════════════════════════════════════════════════════════════════


@mcp.tool()
async def order_query(order_id: str = "", user_id: str = "") -> dict:
    """按订单号或用户ID查询订单信息（内存演示订单表，预置 2 条）。

    Args:
        order_id: 订单号，精确匹配，如 ORD-20260401-001。
        user_id: 用户ID，如 U1001；未提供订单号时按用户返回最近一笔订单。

    Returns:
        dict: 命中时含 found=True 与订单字段；未命中时 found=False 与 message。
    """
    oid = (order_id or "").strip()
    uid = (user_id or "").strip()
    if not oid and not uid:
        return {"found": False, "message": "请提供 order_id 或 user_id 之一。"}

    if oid:
        for order in DEMO_ORDERS:
            if order["order_id"].lower() == oid.lower():
                return {"found": True, **order}
        return {
            "found": False,
            "message": f"未找到订单 {oid}。演示数据可用订单号：{[o['order_id'] for o in DEMO_ORDERS]}",
        }

    matches = [o for o in DEMO_ORDERS if o["user_id"].lower() == uid.lower()]
    if matches:
        return {"found": True, **matches[-1]}  # 返回该用户最近一笔
    return {
        "found": False,
        "message": f"用户 {uid} 名下没有演示订单。演示数据可用用户ID：{[o['user_id'] for o in DEMO_ORDERS]}",
    }


@mcp.tool()
async def knowledge_search(query: str, top_k: int = 3) -> dict:
    """在内存演示知识库中做关键词打分检索（3 条预置文档）。

    Args:
        query: 检索问题或关键词，如 "理财收益多少" "怎么退款" "开户流程"。
        top_k: 最多返回的文档条数，默认 3。

    Returns:
        dict: {"results": [{"content", "source", "score"}], "total", "query"}。
              score 为 0-1 的关键词命中率，按分数降序。
    """
    if not query or not query.strip():
        return {"query": query, "results": [], "total": 0}

    q_tokens = set(_tokenize(query))
    scored: list[dict] = []
    lowered = query.lower()
    for doc in KNOWLEDGE_BASE:
        content = doc["content"]
        d_tokens = set(_tokenize(content))
        if not q_tokens:
            score = 0.0
        else:
            hit = len(q_tokens & d_tokens) / len(q_tokens)
            # 整句子串命中的强加权
            if lowered in content.lower():
                hit = max(hit, 0.99)
            score = round(hit, 4)
        if score > 0:
            scored.append({
                "content": content,
                "source": doc["source"],
                "score": score,
            })

    scored.sort(key=lambda r: r["score"], reverse=True)
    return {
        "query": query,
        "results": scored[: max(1, int(top_k))],
        "total": len(scored),
    }


@mcp.tool()
async def ticket_create(
    title: str,
    description: str,
    priority: Literal["low", "medium", "high", "urgent"] = "medium",
    category: str = "general",
    idempotency_key: str | None = None,
) -> dict:
    """创建客服工单，返回 TK-YYYYMMDD-XXXXXX 格式工单号。

    同一 idempotency_key 重复调用会幂等返回同一工单（不会重复建单）。

    Args:
        title: 工单标题，一句话概括问题。
        description: 问题描述详情。
        priority: 优先级，可选 low / medium / high / urgent，默认 medium。
        category: 业务分类，默认 general。
        idempotency_key: 幂等键；相同键的重复请求返回同一张工单。

    Returns:
        dict: 含 success、ticket_id、status="created"、priority、category、
              created_at、idempotent_replay（是否为幂等重放）。
    """
    key = (idempotency_key or "").strip() or None
    with _TICKET_LOCK:
        if key and key in _TICKETS_BY_KEY:
            return {**_TICKETS_BY_KEY[key], "idempotent_replay": True}

        ticket_id = f"TK-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        record = {
            "success": True,
            "ticket_id": ticket_id,
            "title": title,
            "description": description,
            "priority": priority,
            "category": category,
            "status": "created",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "idempotent_replay": False,
        }
        _TICKETS_BY_ID[ticket_id] = record
        if key:
            _TICKETS_BY_KEY[key] = record
        return dict(record)


@mcp.tool()
async def risk_check(user_id: str, action: str, amount: float = 0.0) -> dict:
    """风控校验：按交易金额阈值给出风险等级与人工复核建议。

    规则：amount > 50000 → high（需人工复核）；amount > 10000 → medium；
    其余（含 0）→ low。

    Args:
        user_id: 用户ID。
        action: 操作/交易类型，如 withdraw、purchase、transfer。
        amount: 涉及金额（元），默认 0.0。

    Returns:
        dict: 含 risk_level（low/medium/high）、requires_manual_review、reason。
    """
    if amount > 50000:
        level, reason = "high", "amount > 50000"
    elif amount > 10000:
        level, reason = "medium", "amount > 10000"
    else:
        level, reason = "low", "amount <= 10000"

    return {
        "user_id": user_id,
        "action": action,
        "amount": amount,
        "risk_level": level,
        "requires_manual_review": level == "high",
        "reason": reason,
    }


# ─── 工具名 → 原生函数映射（tooling.client 进程内直调用；跨 SDK 版本稳定） ───
TOOL_FUNCTIONS: dict[str, Any] = {
    "order_query": order_query,
    "knowledge_search": knowledge_search,
    "ticket_create": ticket_create,
    "risk_check": risk_check,
}


def get_mcp() -> FastMCP:
    """返回进程内共享的 MCP 服务实例（官方 SDK 对象）。"""
    return mcp

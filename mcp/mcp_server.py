"""
MCP工具协议服务端 — Model Context Protocol实现
遵循Anthropic MCP标准，通过JSON-RPC 2.0提供工具注册/发现/调用能力。
支持动态工具扩展，Agent通过统一接口调用外部系统。
包含熔断器（Circuit Breaker）：工具连续失败自动熔断，避免雪崩。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ─── 熔断器配置（Q20）───
CIRCUIT_FAILURE_THRESHOLD = 3    # 连续失败 3 次触发熔断
CIRCUIT_COOLDOWN_SECONDS = 60    # 熔断冷却 60 秒后自动恢复半开


@dataclass
class ToolDefinition:
    """MCP工具定义"""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    requires_auth: bool = False


@dataclass
class ToolCallResult:
    """工具调用结果"""
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class MCPToolServer:
    """
    MCP工具服务端。

    实现 Model Context Protocol 的核心功能：
    1. 工具注册 (Tool Registration)
    2. 工具发现 (Tool Discovery) - Agent可查询可用工具列表
    3. 工具调用 (Tool Invocation) - 通过JSON-RPC 2.0协议调用
    4. 结果返回 (Result Delivery)

    熔断机制（Q20）：
    - 每个工具维护连续失败计数
    - 连续失败 >= CIRCUIT_FAILURE_THRESHOLD → 熔断打开，直接拒绝调用
    - 冷却期后进入半开状态，允许一次探测调用
    - 探测成功 → 熔断关闭；探测失败 → 重新熔断
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._call_log: list[ToolCallResult] = []
        # ── 熔断状态 ──
        self._failure_counts: dict[str, int] = {}      # 工具名 → 连续失败次数
        self._circuit_open_until: dict[str, float] = {}  # 工具名 → 熔断打开截止时间

    def register_tool(self, tool: ToolDefinition) -> None:
        """注册一个MCP工具"""
        self._tools[tool.name] = tool

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str = "general",
        requires_auth: bool = False,
    ) -> Callable:
        """工具注册装饰器"""
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            tool = ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=func,
                category=category,
                requires_auth=requires_auth,
            )
            self._tools[name] = tool
            return func
        return decorator

    # ─── 熔断器方法（Q20）───

    def _is_circuit_open(self, tool_name: str) -> bool:
        """检查工具是否处于熔断打开状态"""
        until = self._circuit_open_until.get(tool_name, 0)
        if until <= 0:
            return False
        if time.time() > until:
            # 冷却结束 → 半开状态，删除标记允许探测
            del self._circuit_open_until[tool_name]
            return False
        return True

    def _trip_circuit(self, tool_name: str) -> None:
        """触发熔断：记录打开时间"""
        self._circuit_open_until[tool_name] = time.time() + CIRCUIT_COOLDOWN_SECONDS
        self._failure_counts[tool_name] = 0  # 熔断打开后重置失败计数

    def get_circuit_status(self) -> dict[str, dict]:
        """获取所有工具的熔断状态（供 /api/metrics 展示）"""
        status = {}
        for name in self._tools:
            state = "closed"
            if self._is_circuit_open(name):
                state = "open"
            elif self._failure_counts.get(name, 0) > 0:
                state = "half-open"
            status[name] = {
                "state": state,
                "consecutive_failures": self._failure_counts.get(name, 0),
                "cooldown_remaining_s": max(0, self._circuit_open_until.get(name, 0) - time.time()),
            }
        return status

    def list_tools(self, category: str | None = None) -> list[dict]:
        """
        工具发现：列出所有可用工具。
        对应MCP的 tools/list 方法。
        """
        tools = []
        for tool in self._tools.values():
            if category and tool.category != category:
                continue
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "category": tool.category,
            })
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """
        工具调用：执行指定工具。
        对应MCP的 tools/call 方法。
        内置熔断器：熔断打开时直接拒绝，冷却后自动恢复。
        """
        import time as _time

        tool = self._tools.get(name)
        if tool is None:
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
            )
            self._call_log.append(result)
            return result

        # ── 熔断检查：打开则直接拒绝 ──
        if self._is_circuit_open(name):
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' 已熔断（连续失败超阈值），冷却中。请稍后重试。",
            )
            self._call_log.append(result)
            return result

        start = _time.time()
        try:
            output = await tool.handler(**arguments)
            duration_ms = (_time.time() - start) * 1000

            # 成功 → 重置失败计数
            self._failure_counts[name] = 0

            result = ToolCallResult(
                tool_name=name,
                success=True,
                result=output,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (_time.time() - start) * 1000

            # 失败 → 连续失败计数 +1，超阈值触发熔断
            self._failure_counts[name] = self._failure_counts.get(name, 0) + 1
            if self._failure_counts[name] >= CIRCUIT_FAILURE_THRESHOLD:
                self._trip_circuit(name)

            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

        self._call_log.append(result)
        return result

    async def handle_jsonrpc(self, request: dict) -> dict:
        """
        处理JSON-RPC 2.0请求。
        MCP协议传输层实现。
        """
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id", 1)

        try:
            if method == "tools/list":
                result = self.list_tools(category=params.get("category"))
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                call_result = await self.call_tool(tool_name, arguments)
                result = {
                    "success": call_result.success,
                    "result": call_result.result,
                    "error": call_result.error,
                }
            elif method == "ping":
                result = {"status": "ok"}
            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": req_id,
                }

            return {"jsonrpc": "2.0", "result": result, "id": req_id}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": req_id,
            }

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        """获取最近的工具调用日志"""
        return [
            {
                "tool": r.tool_name,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "timestamp": r.timestamp,
                "error": r.error,
            }
            for r in self._call_log[-last_n:]
        ]


def create_default_tools(server: MCPToolServer) -> MCPToolServer:
    """注册默认的MCP工具集"""

    @server.register(
        name="order_query",
        description="查询订单信息，支持按订单号或用户ID查询",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
        },
        category="order",
    )
    async def order_query(order_id: str = "", user_id: str = "") -> dict:
        return {
            "order_id": order_id or "ORD-20260401-001",
            "status": "shipped",
            "amount": 299.00,
            "product": "智能理财产品A",
            "created_at": "2026-04-01T10:00:00",
        }

    @server.register(
        name="knowledge_search",
        description="搜索企业知识库，返回相关文档片段",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 3},
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(query: str, top_k: int = 3) -> list[dict]:
        return [
            {"content": f"关于'{query}'的知识库文档片段", "source": "FAQ.md", "score": 0.95},
        ]

    @server.register(
        name="ticket_create",
        description="创建客服工单",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                "category": {"type": "string"},
            },
            "required": ["title", "description"],
        },
        category="ticket",
    )
    async def ticket_create(title: str, description: str, priority: str = "medium", category: str = "general") -> dict:
        import uuid
        return {
            "ticket_id": f"TK-{uuid.uuid4().hex[:8].upper()}",
            "title": title,
            "status": "created",
            "priority": priority,
        }

    @server.register(
        name="risk_check",
        description="风控接口 — 检查交易/操作的风险等级",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "action": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["user_id", "action"],
        },
        category="compliance",
    )
    async def risk_check(user_id: str, action: str, amount: float = 0.0) -> dict:
        risk_level = "low"
        if amount > 50000:
            risk_level = "high"
        elif amount > 10000:
            risk_level = "medium"

        return {
            "user_id": user_id,
            "action": action,
            "risk_level": risk_level,
            "requires_manual_review": risk_level == "high",
        }

    return server

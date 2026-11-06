"""tooling — HeteroAgent 的 MCP 工具层（官方 MCP SDK 实现）。

缺陷 G12 重构：原 mcp/ 目录为自造 JSON-RPC 实现，与 PyPI mcp 包发生 import
遮蔽；本包更名为 tooling/，并改用官方 SDK：
- server.py：FastMCP（mcp 2.x 为 MCPServer）实例 + 4 个 @mcp.tool() 工具；
- client.py：进程内工具调用客户端（熔断保护 + 计时 + 调用日志）；
- breaker.py：可复用线程安全熔断器；
- streamable.py：Streamable HTTP ASGI 应用（可 mount 到 /mcp）。

典型用法::

    from tooling import call_tool, get_mcp
    result = await call_tool("risk_check", {"user_id": "U1", "action": "withdraw", "amount": 60000})
"""

from tooling.breaker import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    CircuitBreaker,
    all_breaker_statuses,
    get_breaker,
    reset_breakers,
)
from tooling.client import (
    ToolGateway,
    call_tool,
    get_call_log,
    get_circuit_status,
    list_tools,
)
from tooling.server import (
    MCP_INSTRUCTIONS,
    MCP_SERVER_NAME,
    TOOL_FUNCTIONS,
    FastMCP,
    get_mcp,
    mcp,
)
from tooling.streamable import create_mcp_asgi_app, mcp_session_lifespan

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "CircuitBreaker",
    "MCP_INSTRUCTIONS",
    "MCP_SERVER_NAME",
    "TOOL_FUNCTIONS",
    "FastMCP",
    "ToolGateway",
    "all_breaker_statuses",
    "call_tool",
    "create_mcp_asgi_app",
    "get_breaker",
    "get_call_log",
    "get_circuit_status",
    "get_mcp",
    "list_tools",
    "mcp",
    "mcp_session_lifespan",
    "reset_breakers",
]

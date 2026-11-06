"""MCP 工具客户端 — 进程内直调 + 熔断保护 + 调用日志（缺陷 G12 重构）。

- call_tool(name, arguments)：进程内直接调用 tooling.server 注册的工具函数
  （通过 TOOL_FUNCTIONS 名称映射，跨官方 SDK 1.x/2.x 稳定），调用前先过
  CircuitBreaker.allow()，失败自动 record_failure、成功 record_success，
  并计量耗时；任何异常都折叠为 {"success": False, "error": ...} 返回，不外抛。
- list_tools()：返回 name / description / inputSchema（JSON Schema）列表。
- get_call_log / get_circuit_status：供 api 调试端点展示（兼容旧接口语义）。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from tooling.breaker import all_breaker_statuses, get_breaker, reset_breakers
from tooling.server import TOOL_FUNCTIONS, get_mcp

__all__ = [
    "ToolGateway",
    "call_tool",
    "get_call_log",
    "get_circuit_status",
    "list_tools",
    "reset_breakers",
]

# 最近调用日志（线程安全的环形缓冲）
_CALL_LOG: deque[dict[str, Any]] = deque(maxlen=500)


async def list_tools() -> list[dict]:
    """工具发现：返回 [{name, description, inputSchema}]。

    走官方 SDK 的服务端 API（list_tools），schema 与 tools/list 线上协议一致
    （inputSchema 为 camelCase 线格式 JSON Schema）。
    """
    tools = await get_mcp().list_tools()
    out: list[dict] = []
    for tool in tools:
        # SDK 1.x 属性为 inputSchema，2.x 为 input_schema（alias 仍是 inputSchema）
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        out.append({
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", "") or "",
            "inputSchema": schema,
        })
    return out


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict:
    """调用一个已注册工具；永不向调用方抛异常。

    流程：熔断检查 → 函数查找 → 计时执行 → 记录成功/失败。
    未知工具与熔断拒绝不计入失败计数（属于调用方错误，不是工具故障）。

    Returns:
        dict: 成功 {"success": True, "tool", "result", "duration_ms", "circuit"}；
              失败 {"success": False, "error", ...}（熔断时附 circuit_open=True）。
    """
    args = dict(arguments or {})
    breaker = get_breaker(name)

    if not breaker.allow():
        result = {
            "success": False,
            "tool": name,
            "error": (
                f"工具 {name} 已熔断（连续失败达到阈值），冷却中，请稍后重试。"
            ),
            "circuit_open": True,
            "circuit": breaker.status(),
        }
        _CALL_LOG.append(_log_entry(result))
        return result

    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        result = {
            "success": False,
            "tool": name,
            "error": (
                f"Tool {name!r} not found. Available: {sorted(TOOL_FUNCTIONS)}"
            ),
        }
        _CALL_LOG.append(_log_entry(result))
        return result

    start = time.perf_counter()
    try:
        payload = await fn(**args)
    except Exception as exc:  # noqa: BLE001 — 统一折叠为失败结果，不外抛
        duration_ms = (time.perf_counter() - start) * 1000
        breaker.record_failure()
        result = {
            "success": False,
            "tool": name,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": round(duration_ms, 3),
            "circuit": breaker.status(),
        }
        _CALL_LOG.append(_log_entry(result))
        return result

    duration_ms = (time.perf_counter() - start) * 1000
    breaker.record_success()
    result = {
        "success": True,
        "tool": name,
        "result": payload,
        "duration_ms": round(duration_ms, 3),
        "circuit": breaker.status(),
    }
    _CALL_LOG.append(_log_entry(result))
    return result


class ToolGateway:
    """兼容旧 app.state.mcp 形态的薄适配层（api/main.py 可选调试端点用）。

    与旧 MCPToolServer 的区别：list_tools / call_tool 现在是协程，
    调用处需要 await（旧实现 list_tools() 为同步）。
    """

    async def list_tools(self, category: str | None = None) -> list[dict]:
        # category 过滤为旧接口遗留参数，演示工具不再区分类别，保留仅为兼容
        _ = category
        return await list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict:
        return await call_tool(name, arguments)

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        return get_call_log(last_n=last_n)

    def get_circuit_status(self) -> dict[str, dict]:
        return get_circuit_status()

    @classmethod
    def create_default(cls) -> ToolGateway:
        """等价于旧 create_default_tools(MCPToolServer())：工具已在 server.py 注册。"""
        return cls()


def get_call_log(last_n: int = 100) -> list[dict]:
    """最近 last_n 条调用日志（兼容原 MCPToolServer.get_call_log 语义）。"""
    entries = list(_CALL_LOG)
    return entries[-max(1, last_n):]


def get_circuit_status() -> dict[str, dict]:
    """全部熔断器状态（兼容原 MCPToolServer.get_circuit_status 语义）。"""
    return all_breaker_statuses()


def _log_entry(result: dict) -> dict:
    return {
        "tool": result.get("tool"),
        "success": bool(result.get("success")),
        "error": result.get("error"),
        "duration_ms": result.get("duration_ms", 0.0),
    }

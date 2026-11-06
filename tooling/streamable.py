"""MCP Streamable HTTP 传输 — 可挂载到宿主 FastAPI/Starlette 的 ASGI 应用。

create_mcp_asgi_app() 返回官方 SDK 的 Streamable HTTP ASGI 应用
（FastMCP/MCPServer.streamable_http_app()）。默认参数面向「多副本无状态部署」：

    from tooling.streamable import create_mcp_asgi_app, mcp_session_lifespan

    app = FastAPI(lifespan=...)  # 在宿主 lifespan 中: async with mcp_session_lifespan(): yield
    app.mount("/mcp", create_mcp_asgi_app())

多副本无状态部署要点（重要）：
1. 会话托管：streamable_http_app() 内部依赖 session_manager 的 task group，
   必须在宿主应用 lifespan 内 `async with mcp_session_lifespan():`（即
   session_manager.run()）启动，否则请求报 RuntimeError: Task group is not
   initialized。
2. 无粘性会话：默认 stateless_http=True 每请求独立会话，不在内存保存 MCP
   会话，多副本 + 负载均衡无需 session 亲和（sticky session）；Mcp-Session-Id
   头也不会被要求回传。
3. json_response=True：POST 响应使用普通 application/json 而非 SSE 流，
   兼容只读 Body 的 LB/网关健康检查与代理缓冲。
4. DNS rebinding 防护：SDK 默认仅放行 127.0.0.1/localhost 的 Host 头；
   生产部署在反代后面时用 enable_dns_rebinding_protection=False 或
   allowed_hosts/allowed_origins 放行真实域名（代理场景校验会误伤）。
5. 事件重放：如需 GET 长连接流式推送与断线重连，多副本要共享外部
   event_store（Redis 等），否则不同副本无法重放彼此的事件；纯请求-响应
   用法（本仓库演示工具）不需要。
6. 路径：默认 streamable_http_path="/"，使宿主 mount("/mcp", app) 后实际
   端点即 /mcp；独立运行（不挂载）时可传 streamable_http_path="/mcp"。
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from tooling.server import get_mcp

__all__ = ["create_mcp_asgi_app", "mcp_session_lifespan"]


def create_mcp_asgi_app(
    *,
    stateless_http: bool = True,
    json_response: bool = True,
    streamable_http_path: str = "/",
    enable_dns_rebinding_protection: bool | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    event_store: Any | None = None,
) -> Any:
    """返回可 mount 的 Streamable HTTP ASGI 应用（Starlette 实例）。

    参数按当前安装的官方 SDK 版本自适应：不支持的参数自动忽略，
    因此同一调用在 mcp 1.x（FastMCP.streamable_http_app() 无参）与
    mcp 2.x（支持上述关键字参数）下均可工作。
    """
    mcp = get_mcp()
    app_factory = mcp.streamable_http_app  # 官方 SDK 方法名（两代版本一致）
    supported = inspect.signature(app_factory).parameters

    kwargs: dict[str, Any] = {}
    if "stateless_http" in supported:
        kwargs["stateless_http"] = stateless_http
    if "json_response" in supported:
        kwargs["json_response"] = json_response
    if "streamable_http_path" in supported:
        kwargs["streamable_http_path"] = streamable_http_path
    if event_store is not None and "event_store" in supported:
        kwargs["event_store"] = event_store

    if enable_dns_rebinding_protection is False or allowed_hosts or allowed_origins:
        if "transport_security" in supported:
            kwargs["transport_security"] = _transport_security(
                enable_dns_rebinding_protection, allowed_hosts, allowed_origins
            )

    return app_factory(**kwargs)


def _transport_security(
    enable_dns_rebinding_protection: bool | None,
    allowed_hosts: list[str] | None,
    allowed_origins: list[str] | None,
) -> Any:
    """构造 SDK 的 TransportSecuritySettings（类缺失时返回 None 跳过）。"""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # 极老版本无该类
        return None
    kwargs: dict[str, Any] = {}
    if enable_dns_rebinding_protection is not None:
        kwargs["enable_dns_rebinding_protection"] = enable_dns_rebinding_protection
    if allowed_hosts:
        kwargs["allowed_hosts"] = allowed_hosts
    if allowed_origins:
        kwargs["allowed_origins"] = allowed_origins
    return TransportSecuritySettings(**kwargs)


@asynccontextmanager
async def mcp_session_lifespan() -> AsyncIterator[None]:
    """宿主 lifespan 内启动 MCP 会话管理器。

    用法（FastAPI / Starlette）::

        @asynccontextmanager
        async def lifespan(app):
            async with mcp_session_lifespan():
                yield

    未运行它时，mount 后的 /mcp 端点会报
    "RuntimeError: Task group is not initialized. Make sure to use run()."。
    """
    manager = getattr(get_mcp(), "session_manager", None)
    if manager is None:
        yield
        return
    async with manager.run():
        yield

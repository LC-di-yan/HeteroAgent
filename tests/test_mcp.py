"""tooling 官方 MCP SDK 集成测试（全程离线，缺陷 G12 回归防护）。

覆盖三件事：
1. 真实 MCP 协议：用官方 SDK 的进程内内存传输跑 initialize/tools/list/tools/call；
   - SDK 1.x：mcp.shared.memory.create_connected_server_and_client_session
   - SDK 2.x：mcp.Client(server)（1.x 助手在 2.x 中已移除）
2. tooling.client 的高层封装：call_tool/list_tools、熔断器行为；
3. tooling.streamable 的 Streamable HTTP ASGI 应用可 mount 到 /mcp。

无网络、无 API key 依赖。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import mcp
import pytest

import tooling.client
from tooling.breaker import CircuitBreaker, get_breaker
from tooling.server import MCP_SERVER_NAME, get_mcp

EXPECTED_TOOLS = {"order_query", "knowledge_search", "ticket_create", "risk_check"}
EXPECTED_REQUIRED = {
    "order_query": [],
    "knowledge_search": ["query"],
    "ticket_create": ["title", "description"],
    "risk_check": ["user_id", "action"],
}
TICKET_ID_RE = re.compile(r"^TK-\d{8}-[A-F0-9]{6}$")

# ─── 官方 SDK 内存传输兼容层 ───────────────────────────────────────

try:  # mcp SDK 1.x
    from mcp.shared.memory import (
        create_connected_server_and_client_session as _memory_session,
    )
except ImportError:  # mcp SDK 2.x 移除了该助手，改用顶层 mcp.Client
    _memory_session = None


@asynccontextmanager
async def in_memory_mcp_session() -> AsyncIterator[Any]:
    """官方内存传输：与真实 MCP 协议（initialize/list/call）完全一致。"""
    if _memory_session is not None:
        # v1 助手只收 lowlevel.Server；FastMCP 需取其内部 _mcp_server（v2 无此属性）
        server_obj = getattr(get_mcp(), "_mcp_server", get_mcp())
        async with _memory_session(server_obj) as session:
            yield session
    else:
        async with mcp.Client(get_mcp()) as client:
            yield client


def tool_input_schema(tool: Any) -> dict:
    """跨版本取 inputSchema（1.x 属性 inputSchema；2.x 属性 input_schema）。"""
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    if schema is None and hasattr(tool, "model_dump"):
        dump = tool.model_dump(by_alias=True)
        schema = dump.get("inputSchema") or dump.get("input_schema") or {}
    return schema or {}


def call_payload(result: Any) -> dict:
    """从 CallToolResult 提取工具返回的 dict（结构化优先，JSON 文本兜底）。"""
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured
    text = ""
    for block in result.content or []:
        candidate = getattr(block, "text", None)
        if candidate:
            text = candidate
            break
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def call_failed(result: Any) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


@pytest.fixture(autouse=True)
def _fresh_breakers() -> Iterator[None]:
    """每个测试独立熔断状态，避免跨测试污染。"""
    from tooling import breaker as breaker_mod

    breaker_mod.reset_breakers()
    yield
    breaker_mod.reset_breakers()


# ══════════════════════════════════════════════════════════════════
# G12 回归防护：不再存在本地 mcp/ 遮蔽
# ══════════════════════════════════════════════════════════════════


def test_no_local_mcp_shadow() -> None:
    """import mcp 必须命中 PyPI 官方包，而不是仓库内的自造目录。"""
    repo_root = Path(__file__).resolve().parents[1]
    assert not (repo_root / "mcp").exists(), "本地 mcp/ 目录应已删除（G12）"
    assert "site-packages" in str(Path(mcp.__file__).resolve()), (
        f"import mcp 应来自 site-packages，实际是 {mcp.__file__}"
    )
    # 官方 SDK 顶层应能解析 server / client 两侧入口
    assert get_mcp() is not None


# ══════════════════════════════════════════════════════════════════
# 真实 MCP 协议（官方内存传输）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tools_list_returns_four_tools_with_required_fields() -> None:
    async with in_memory_mcp_session() as session:
        listed = await session.list_tools()
    tools = {t.name: t for t in listed.tools}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        schema = tool_input_schema(tool)
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict) and schema["properties"], name
        # 必填参数的工具必须携带 required 字段
        assert schema.get("required", []) == EXPECTED_REQUIRED[name]
    # 抽查：Literal 注解应生成 enum
    assert tool_input_schema(tools["ticket_create"])["properties"]["priority"]["enum"] == [
        "low", "medium", "high", "urgent"
    ]


@pytest.mark.asyncio
async def test_ticket_create_returns_formatted_id() -> None:
    async with in_memory_mcp_session() as session:
        result = await session.call_tool(
            "ticket_create",
            {"title": "充值未到账", "description": "用户反映充值 200 元未到账"},
        )
    assert not call_failed(result)
    payload = call_payload(result)
    assert TICKET_ID_RE.match(payload["ticket_id"]), payload["ticket_id"]
    assert payload["status"] == "created"
    assert payload["priority"] == "medium"  # 默认值


@pytest.mark.asyncio
async def test_ticket_create_idempotency_key_replays_same_ticket() -> None:
    key = "g12-test-idem-001"
    async with in_memory_mcp_session() as session:
        first = call_payload(await session.call_tool(
            "ticket_create",
            {"title": "退款咨询", "description": "想退第2笔订单", "idempotency_key": key},
        ))
        second = call_payload(await session.call_tool(
            "ticket_create",
            {"title": "退款咨询（重复推送）", "description": "同问题重试", "idempotency_key": key},
        ))
    assert first["ticket_id"] == second["ticket_id"]
    assert second["idempotent_replay"] is True
    assert TICKET_ID_RE.match(first["ticket_id"])


@pytest.mark.asyncio
async def test_order_query_miss_returns_found_false() -> None:
    async with in_memory_mcp_session() as session:
        miss = call_payload(await session.call_tool(
            "order_query", {"order_id": "ORD-DOES-NOT-EXIST"}
        ))
        empty = call_payload(await session.call_tool("order_query", {}))
    assert miss["found"] is False
    assert "message" in miss
    assert empty["found"] is False  # 两个参数都为空时也视为未命中


@pytest.mark.asyncio
async def test_risk_check_amount_tiers() -> None:
    cases = [
        (60000.0, "high", True),
        (20000.0, "medium", False),
        (50000.0, "medium", False),  # 阈值是严格大于
        (100.0, "low", False),
        (0.0, "low", False),
    ]
    async with in_memory_mcp_session() as session:
        for amount, level, review in cases:
            payload = call_payload(await session.call_tool(
                "risk_check",
                {"user_id": "U1001", "action": "withdraw", "amount": amount},
            ))
            assert payload["risk_level"] == level, (amount, payload)
            assert payload["requires_manual_review"] is review, (amount, payload)


# ══════════════════════════════════════════════════════════════════
# tooling.client：进程内调用封装
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_client_list_tools_exposes_schema() -> None:
    tools = await tooling.client.list_tools()
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"].get("type") == "object"
        assert "description" in tool


@pytest.mark.asyncio
async def test_client_call_tool_success_and_timing() -> None:
    result = await tooling.client.call_tool(
        "knowledge_search", {"query": "理财收益", "top_k": 2}
    )
    assert result["success"] is True
    assert result["duration_ms"] >= 0.0
    results = result["result"]["results"]
    assert results and results[0]["score"] > 0
    assert {"content", "source", "score"} <= set(results[0])


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error_without_raising() -> None:
    result = await tooling.client.call_tool("no_such_tool", {"x": 1})
    assert result["success"] is False
    assert "not found" in result["error"]
    # 未知工具是调用方错误，不应计为工具故障（熔断器仍闭合）
    assert get_breaker("no_such_tool").allow() is True


@pytest.mark.asyncio
async def test_call_tool_never_raises_on_bad_arguments() -> None:
    # 缺少必填参数 / 多传未知参数 → 折叠为 success=False
    missing = await tooling.client.call_tool("risk_check", {"user_id": "U1"})
    assert missing["success"] is False and missing["error"]


def test_circuit_breaker_opens_after_three_failures_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = CircuitBreaker("risk_check", failure_threshold=3, cooldown_seconds=60)
    now = {"value": time.time()}
    monkeypatch.setattr(time, "time", lambda: now["value"])  # 模拟时钟

    assert breaker.status()["state"] == "closed"
    assert breaker.allow() is True

    for _ in range(3):
        breaker.record_failure()

    # 连续 3 次失败 → 熔断打开，直接拒绝
    assert breaker.status()["state"] == "open"
    assert breaker.allow() is False

    # 冷却 60 秒未到 → 仍拒绝
    now["value"] += 30
    assert breaker.allow() is False

    # 冷却结束 → 半开，放行探测；探测成功 → 关闭
    now["value"] += 61
    assert breaker.status()["state"] == "half-open"
    assert breaker.allow() is True
    breaker.record_success()
    assert breaker.status()["state"] == "closed"
    assert breaker.allow() is True


def test_circuit_breaker_half_open_probe_failure_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = CircuitBreaker("risk_check", failure_threshold=3, cooldown_seconds=60)
    now = {"value": time.time()}
    monkeypatch.setattr(time, "time", lambda: now["value"])

    for _ in range(3):
        breaker.record_failure()
    now["value"] += 61
    assert breaker.allow() is True          # 半开放行探测
    breaker.record_failure()                # 探测失败 → 重新打开并重新计时
    assert breaker.status()["state"] == "open"
    assert breaker.allow() is False
    now["value"] += 61
    assert breaker.allow() is True


@pytest.mark.asyncio
async def test_client_circuit_breaker_blocks_after_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_tool 层面：工具连续抛异常 3 次后，第 4 次直接被熔断拒绝。"""
    from tooling import breaker as breaker_mod

    now = {"value": time.time()}
    monkeypatch.setattr(breaker_mod.time, "time", lambda: now["value"])

    async def flaky() -> dict:
        raise RuntimeError("upstream down")

    import tooling.server as server_mod
    monkeypatch.setitem(server_mod.TOOL_FUNCTIONS, "flaky_tool", flaky)

    for i in range(3):
        result = await tooling.client.call_tool("flaky_tool", {})
        assert result["success"] is False
        assert "upstream down" in result["error"]
        assert result["circuit"]["state"] == ("open" if i == 2 else "closed")

    blocked = await tooling.client.call_tool("flaky_tool", {})
    assert blocked["success"] is False
    assert blocked.get("circuit_open") is True
    assert "熔断" in blocked["error"]


# ══════════════════════════════════════════════════════════════════
# tooling.streamable：Streamable HTTP 挂载（离线，ASGI 内 Transport）
# ══════════════════════════════════════════════════════════════════


def test_streamable_http_app_mounts_at_mcp() -> None:
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from tooling.streamable import create_mcp_asgi_app, mcp_session_lifespan

    sub_app = create_mcp_asgi_app(enable_dns_rebinding_protection=False)

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        async with mcp_session_lifespan():
            yield

    parent = Starlette(lifespan=lifespan, routes=[Mount("/mcp", app=sub_app)])

    def rpc(method: str, params: dict, rpc_id: int) -> dict:
        return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}

    init_params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    }
    with TestClient(parent) as client:
        init_resp = client.post("/mcp", json=rpc("initialize", init_params, 1))
        assert init_resp.status_code == 200
        assert init_resp.json()["result"]["serverInfo"]["name"] == MCP_SERVER_NAME

        list_resp = client.post("/mcp", json=rpc("tools/list", {}, 2))
        assert list_resp.status_code == 200
        wire_names = {t["name"] for t in list_resp.json()["result"]["tools"]}
        assert wire_names == EXPECTED_TOOLS

        call_resp = client.post(
            "/mcp",
            json=rpc(
                "tools/call",
                {"name": "risk_check", "arguments": {"user_id": "U9", "action": "withdraw", "amount": 60000}},
                3,
            ),
        )
        assert call_resp.status_code == 200
        content_text = call_resp.json()["result"]["content"][0]["text"]
        assert json.loads(content_text)["risk_level"] == "high"

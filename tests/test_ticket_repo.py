"""统一工单仓储测试（偿还 ADR-0007 双存储分裂）。"""
from __future__ import annotations

import asyncio

import pytest

from agents.ticket_handler import TicketStore
from persistence.ticket_repo import TicketRepository, get_default_repo, set_default_repo
from tooling.server import set_ticket_repo


@pytest.fixture(autouse=True)
def _isolate_ticket_repo():
    """每个测试用独立仓储，避免全局状态泄漏。"""
    repo = TicketRepository()
    set_ticket_repo(repo)
    set_default_repo(repo)
    yield repo
    set_ticket_repo(None)
    set_default_repo(None)


def test_repo_create_and_get():
    repo = TicketRepository()
    rec = repo.create(title="退款", description="7天内", priority="high", user_id="u1")
    assert rec["ticket_id"].startswith("TK-")
    assert rec["status"] == "created"
    fetched = repo.get(rec["ticket_id"])
    assert fetched is not None
    assert fetched["title"] == "退款"


def test_idempotency():
    repo = TicketRepository()
    a = repo.create(title="退款", idempotency_key="k1")
    b = repo.create(title="退款", idempotency_key="k1")
    assert a["ticket_id"] == b["ticket_id"]
    assert b["idempotent_replay"] is True
    assert repo.count() == 1


def test_query_by_user():
    repo = TicketRepository()
    repo.create(title="A", user_id="u1")
    repo.create(title="B", user_id="u2")
    repo.create(title="C", user_id="u1")
    assert len(repo.query_by_user("u1")) == 2
    assert len(repo.query_by_user("u2")) == 1


def test_update_status():
    repo = TicketRepository()
    rec = repo.create(title="退款")
    updated = repo.update_status(rec["ticket_id"], "resolved")
    assert updated["status"] == "resolved"
    assert repo.get(rec["ticket_id"])["status"] == "resolved"


def test_mcp_tool_and_ticketstore_share_same_repo(_isolate_ticket_repo):
    """核心回归：MCP 工具建的工单，TicketStore 能查到（反之亦然）。"""
    from tooling.server import ticket_create

    # MCP 工具路径创建
    result = asyncio.run(ticket_create(title="工具建的工单", description="via MCP"))
    ticket_id = result["ticket_id"]

    # 降级路径（TicketStore）能查到
    store = TicketStore(repo=_isolate_ticket_repo)
    found = store.query(ticket_id)
    assert found is not None
    assert found["title"] == "工具建的工单"


def test_ticketstore_create_visible_to_mcp_repo(_isolate_ticket_repo):
    """反向：TicketStore 建的工单，共享仓储能查到。"""
    store = TicketStore(repo=_isolate_ticket_repo)
    rec = store.create(ticket_type="refund", priority="high",
                       summary="降级建的", details="via fallback", user_id="u1")
    ticket_id = rec["ticket_id"]

    # 共享仓储能查到
    found = _isolate_ticket_repo.get(ticket_id)
    assert found is not None
    assert found["summary"] == "降级建的"


def test_ticketstore_without_repo_keeps_old_behavior():
    """未注入 repo 时保持旧的进程内行为（测试/演示兼容）。"""
    store = TicketStore()
    rec = store.create(ticket_type="general", priority="low",
                       summary="独立", details="test", user_id="u1")
    assert store.query(rec["ticket_id"]) is not None
    # 全局仓储里查不到（因为是独立实例）
    assert get_default_repo().get(rec["ticket_id"]) is None


def test_schema_compat_both_field_names():
    """统一 schema 兼容两边的字段名。"""
    repo = TicketRepository()
    rec = repo.create(title="标题", description="描述", ticket_type="refund",
                      summary="摘要", details="详情", category="claim")
    # title/summary 都有值（title 优先）
    assert rec["title"] == "标题"
    assert rec["summary"] == "标题"
    # type/category 都有值（ticket_type 优先）
    assert rec["type"] == "refund"
    assert rec["category"] == "refund"

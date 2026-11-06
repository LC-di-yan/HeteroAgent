"""Redis 审批存储测试（多副本路径，fakeredis）。"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from security.approvals import RedisApprovalStore, verify_chain


def _store(ttl: int = 3600) -> RedisApprovalStore:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisApprovalStore("redis://unused", ttl_seconds=ttl, client=client)


def _request(store: RedisApprovalStore, **overrides):
    payload = dict(
        tenant_id="t1", session_id="s1", trace_id="tr1", user_message="保证收益",
        agent_responses={"knowledge_rag": "已脱敏"}, violations=["违规"], risk_level="high",
        intent="knowledge_rag",
    )
    payload.update(overrides)
    return asyncio.run(store.new_request(**payload))


def test_create_and_get_roundtrip():
    store = _store()
    rec = _request(store)
    fetched = asyncio.run(store.get(rec.approval_id))
    assert fetched is not None
    assert fetched.session_id == "s1"
    assert fetched.state == "pending"


def test_list_pending_returns_created():
    store = _store()
    rec = _request(store)
    pending = asyncio.run(store.list_pending())
    assert [r.approval_id for r in pending] == [rec.approval_id]


def test_decide_writes_audit_chain():
    store = _store()
    rec = _request(store)
    decided, row = asyncio.run(store.decide(rec.approval_id, "approved", "key:admin", "ok"))
    assert decided.state == "approved"
    assert row["row_hash"]

    rows = asyncio.run(store.audit_rows())
    assert len(rows) == 1
    assert verify_chain(rows) is True

    # 已决策的单子不再出现在待审批列表
    assert asyncio.run(store.list_pending()) == []


def test_double_decision_rejected():
    store = _store()
    rec = _request(store)
    asyncio.run(store.decide(rec.approval_id, "approved", "a", None))
    with pytest.raises(ValueError):
        asyncio.run(store.decide(rec.approval_id, "rejected", "a", None))


def test_unknown_id_raises_keyerror():
    store = _store()
    with pytest.raises(KeyError):
        asyncio.run(store.decide("nope", "approved", "a", None))


def test_expired_record_marked():
    store = _store(ttl=-1)  # 立即过期
    rec = _request(store)
    fetched = asyncio.run(store.get(rec.approval_id))
    assert fetched.state == "expired"
    with pytest.raises(ValueError):
        asyncio.run(store.decide(rec.approval_id, "approved", "a", None))

"""审批存储与审计链测试（T-P0-03）。"""
from __future__ import annotations

import asyncio
import time

import pytest

from security.approvals import (
    GENESIS_HASH,
    ApprovalRecord,
    MemoryApprovalStore,
    build_audit_row,
    verify_chain,
)


def _record(**overrides) -> ApprovalRecord:
    base = dict(
        approval_id="a-1", tenant_id="t1", session_id="s1", trace_id="tr1",
        user_message="帮我保证收益", agent_responses={"knowledge_rag": "已脱敏回复"},
        violations=["违规词"], risk_level="high", intent="knowledge_rag",
        created_at=time.time(), expires_at=time.time() + 3600,
    )
    base.update(overrides)
    return ApprovalRecord(**base)


def test_audit_chain_roundtrip():
    store = MemoryApprovalStore()
    asyncio.run(store.create(_record()))
    rec1, row1 = asyncio.run(store.decide("a-1", "approved", "key:admin", "ok"))
    assert rec1.state == "approved"
    assert row1["prev_hash"] == GENESIS_HASH
    rows = asyncio.run(store.audit_rows())
    assert verify_chain(rows) is True


def test_audit_chain_detects_tampering():
    store = MemoryApprovalStore()
    asyncio.run(store.create(_record()))
    asyncio.run(store.decide("a-1", "approved", "key:admin", "ok"))
    rows = asyncio.run(store.audit_rows())
    rows[0]["decision"] = "rejected"  # 篡改
    assert verify_chain(rows) is False


def test_double_decision_rejected():
    store = MemoryApprovalStore()
    asyncio.run(store.create(_record()))
    asyncio.run(store.decide("a-1", "approved", "key:admin", "ok"))
    with pytest.raises(ValueError):
        asyncio.run(store.decide("a-1", "rejected", "key:admin", "no"))


def test_unknown_id_raises_keyerror():
    store = MemoryApprovalStore()
    with pytest.raises(KeyError):
        asyncio.run(store.decide("missing", "approved", "x", None))


def test_expired_record_not_decidable():
    store = MemoryApprovalStore(ttl_seconds=-1)
    rec = asyncio.run(store.new_request(
        tenant_id="t1", session_id="s1", trace_id="tr1", user_message="m",
        agent_responses={}, violations=[], risk_level="high", intent="knowledge_rag",
    ))
    with pytest.raises(ValueError):
        asyncio.run(store.decide(rec.approval_id, "approved", "x", None))


def test_pending_list_sorted_by_created_at():
    store = MemoryApprovalStore()
    asyncio.run(store.create(_record(approval_id="a-1")))
    asyncio.run(store.create(_record(approval_id="b-2")))
    pending = asyncio.run(store.list_pending())
    assert [r.approval_id for r in pending] == ["a-1", "b-2"]


def test_build_audit_row_fields():
    row = build_audit_row(GENESIS_HASH, {"decision": "approved", "actor": "a"})
    assert row["prev_hash"] == GENESIS_HASH
    assert len(row["row_hash"]) == 64
    assert row["created_at"] > 0

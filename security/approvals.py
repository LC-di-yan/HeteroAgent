"""人工审批存储 + 防篡改审计链（T-P0-03）。

设计要点：
- 审批状态外置（Redis 或内存），进程重启不丢（Redis 模式）；
- 审计链：每条决策记录 prev_hash / row_hash，sha256 链式，可离线校验；
- 审批通过不直接外发被拦截原文——由调用方用 sanitize_output 清洗后返回。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

AUDIT_PREFIX = "heteroagent:approval:audit"
RECORD_PREFIX = "heteroagent:approval:rec"
PENDING_KEY = "heteroagent:approval:pending"
GENESIS_HASH = "0" * 64

VALID_STATES = {"pending", "approved", "rejected", "expired"}


@dataclass
class ApprovalRecord:
    """一条待审批请求。agent_responses 必须是已脱敏内容（合规节点产出）。"""

    approval_id: str
    tenant_id: str
    session_id: str
    trace_id: str
    user_message: str
    agent_responses: dict[str, str]
    violations: list[str]
    risk_level: str
    intent: str
    created_at: float
    expires_at: float
    state: str = "pending"
    decided_at: float | None = None
    decided_by: str | None = None
    decision_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    """row_hash = sha256(prev_hash + canonical(entry))。"""
    return hashlib.sha256((prev_hash + _canonical(entry)).encode("utf-8")).hexdigest()


def build_audit_row(prev_hash: str, entry: dict[str, Any]) -> dict[str, Any]:
    """构造审计行；created_at 先落定再参与哈希，保证 verify_chain 可复算。"""
    row = dict(entry)
    row.setdefault("created_at", time.time())
    row["prev_hash"] = prev_hash
    row["row_hash"] = chain_hash(prev_hash, {k: v for k, v in row.items() if k != "prev_hash"})
    return row


def verify_chain(rows: list[dict[str, Any]]) -> bool:
    """校验审计链完整性；任意一条被篡改则返回 False。"""
    prev = GENESIS_HASH
    for row in rows:
        entry = {k: v for k, v in row.items() if k not in ("prev_hash", "row_hash")}
        if row.get("prev_hash") != prev:
            return False
        if row.get("row_hash") != chain_hash(prev, entry):
            return False
        prev = row["row_hash"]
    return True


class ApprovalStore(Protocol):
    async def create(self, record: ApprovalRecord) -> ApprovalRecord: ...
    async def get(self, approval_id: str) -> ApprovalRecord | None: ...
    async def list_pending(self) -> list[ApprovalRecord]: ...
    async def decide(
        self, approval_id: str, decision: str, actor: str, reason: str | None = None
    ) -> tuple[ApprovalRecord, dict[str, Any]]: ...
    async def audit_rows(self) -> list[dict[str, Any]]: ...


def _new_record(
    *, tenant_id: str, session_id: str, trace_id: str, user_message: str,
    agent_responses: dict[str, str], violations: list[str], risk_level: str,
    intent: str, ttl_seconds: int,
) -> ApprovalRecord:
    now = time.time()
    return ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        session_id=session_id,
        trace_id=trace_id,
        user_message=user_message,
        agent_responses=agent_responses,
        violations=violations,
        risk_level=risk_level,
        intent=intent,
        created_at=now,
        expires_at=now + ttl_seconds,
    )


class MemoryApprovalStore:
    """进程内实现：单机/本地开发与测试。生产用 RedisApprovalStore。"""

    def __init__(self, ttl_seconds: int = 86400):
        self._ttl = ttl_seconds
        self._records: dict[str, ApprovalRecord] = {}
        self._audit: list[dict[str, Any]] = []

    async def create(self, record: ApprovalRecord) -> ApprovalRecord:
        self._records[record.approval_id] = record
        return record

    async def new_request(self, **kwargs: Any) -> ApprovalRecord:
        ttl = kwargs.pop("ttl_seconds", self._ttl)
        return await self.create(_new_record(ttl_seconds=ttl, **kwargs))

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        rec = self._records.get(approval_id)
        if rec and rec.state == "pending" and time.time() > rec.expires_at:
            rec.state = "expired"
        return rec

    async def list_pending(self) -> list[ApprovalRecord]:
        out = []
        for rec in self._records.values():
            if rec.state == "pending" and time.time() > rec.expires_at:
                rec.state = "expired"
            if rec.state == "pending":
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)

    async def decide(
        self, approval_id: str, decision: str, actor: str, reason: str | None = None
    ) -> tuple[ApprovalRecord, dict[str, Any]]:
        rec = await self.get(approval_id)
        if rec is None:
            raise KeyError(approval_id)
        if rec.state != "pending":
            raise ValueError(f"审批单状态为 {rec.state}，不可重复决策")
        if decision not in ("approved", "rejected"):
            raise ValueError("decision 必须是 approved 或 rejected")
        rec.state = decision
        rec.decided_at = time.time()
        rec.decided_by = actor
        rec.decision_reason = reason
        row = build_audit_row(
            self._audit[-1]["row_hash"] if self._audit else GENESIS_HASH,
            {
                "approval_id": approval_id,
                "decision": decision,
                "actor": actor,
                "reason": reason or "",
                "risk_level": rec.risk_level,
                "violations": rec.violations,
            },
        )
        self._audit.append(row)
        return rec, row

    async def audit_rows(self) -> list[dict[str, Any]]:
        return list(self._audit)


class RedisApprovalStore:
    """Redis 实现：跨进程/多副本共享，审批单带 TTL。"""

    def __init__(self, url: str, ttl_seconds: int = 86400, client: Any = None):
        self._url = url
        self._ttl = ttl_seconds
        self._client = client
        self._memory_fallback: MemoryApprovalStore | None = None

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            client = aioredis.from_url(self._url, decode_responses=True)
            await client.ping()
            self._client = client
        return self._client

    async def create(self, record: ApprovalRecord) -> ApprovalRecord:
        client = await self._redis()
        # Redis 不接受非正的 EX；ttl<=0 视为"立即过期"，仍短暂保留以便读取到 expired 状态
        ttl = self._ttl if self._ttl > 0 else 1
        await client.set(f"{RECORD_PREFIX}:{record.approval_id}",
                         json.dumps(record.to_dict(), ensure_ascii=False), ex=ttl)
        await client.zadd(PENDING_KEY, {record.approval_id: record.created_at})
        await client.expire(PENDING_KEY, self._ttl)
        return record

    async def new_request(self, **kwargs: Any) -> ApprovalRecord:
        ttl = kwargs.pop("ttl_seconds", self._ttl)
        return await self.create(_new_record(ttl_seconds=ttl, **kwargs))

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        client = await self._redis()
        raw = await client.get(f"{RECORD_PREFIX}:{approval_id}")
        if not raw:
            return None
        rec = ApprovalRecord.from_dict(json.loads(raw))
        if rec.state == "pending" and time.time() > rec.expires_at:
            rec.state = "expired"
            await self._save(rec)
        return rec

    async def _save(self, rec: ApprovalRecord) -> None:
        client = await self._redis()
        ttl = self._ttl if self._ttl > 0 else 1
        await client.set(f"{RECORD_PREFIX}:{rec.approval_id}",
                         json.dumps(rec.to_dict(), ensure_ascii=False), ex=ttl)

    async def list_pending(self) -> list[ApprovalRecord]:
        client = await self._redis()
        ids = await client.zrange(PENDING_KEY, 0, -1)
        out: list[ApprovalRecord] = []
        for aid in ids:
            rec = await self.get(aid)
            if rec and rec.state == "pending":
                out.append(rec)
            else:
                await client.zrem(PENDING_KEY, aid)
        return out

    async def decide(
        self, approval_id: str, decision: str, actor: str, reason: str | None = None
    ) -> tuple[ApprovalRecord, dict[str, Any]]:
        rec = await self.get(approval_id)
        if rec is None:
            raise KeyError(approval_id)
        if rec.state != "pending":
            raise ValueError(f"审批单状态为 {rec.state}，不可重复决策")
        rec.state = decision
        rec.decided_at = time.time()
        rec.decided_by = actor
        rec.decision_reason = reason
        await self._save(rec)
        client = await self._redis()
        await client.zrem(PENDING_KEY, approval_id)

        last = await client.lindex(AUDIT_PREFIX, -1)
        prev = json.loads(last)["row_hash"] if last else GENESIS_HASH
        row = build_audit_row(prev, {
            "approval_id": approval_id,
            "decision": decision,
            "actor": actor,
            "reason": reason or "",
            "risk_level": rec.risk_level,
            "violations": rec.violations,
        })
        await client.rpush(AUDIT_PREFIX, json.dumps(row, ensure_ascii=False))
        return rec, row

    async def audit_rows(self) -> list[dict[str, Any]]:
        client = await self._redis()
        raw = await client.lrange(AUDIT_PREFIX, 0, -1)
        return [json.loads(item) for item in raw]


def create_approval_store(settings: Any) -> ApprovalStore:
    """按配置创建审批存储。redis 模式下连接失败直接抛错（不做静默降级）。"""
    kind = getattr(settings, "approval_store", "memory")
    ttl = int(getattr(settings, "approval_ttl_seconds", 86400))
    if kind == "redis":
        logger.info("审批存储: Redis (%s)", getattr(settings, "redis_url", ""))
        return RedisApprovalStore(settings.redis_url, ttl_seconds=ttl)
    logger.info("审批存储: 内存（单进程；多副本请设 APPROVAL_STORE=redis）")
    return MemoryApprovalStore(ttl_seconds=ttl)

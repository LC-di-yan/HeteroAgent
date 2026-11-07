"""统一工单仓储（偿还 ADR-0007 的双存储技术债）。

问题：MCP 工具路径写 tooling/server.py 的模块级 dict，降级路径写 TicketStore 的实例 dict，
两者互不可见——工具建的工单在降级路径查不到，反之亦然。

做法：一个共享 TicketRepository 实例，启动时注入到两条路径。
统一 schema 兼容两边的字段名（title/summary、description/details、category/type）。
生产环境替换为数据库实现时只需换这一个类。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any


class TicketRepository:
    """进程内工单仓储（线程安全）。生产环境替换为 DB 实现。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_key: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _new_id() -> str:
        return f"TK-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"

    def create(
        self,
        *,
        title: str = "",
        description: str = "",
        ticket_type: str = "general",
        priority: str = "medium",
        user_id: str = "",
        idempotency_key: str | None = None,
        # 兼容旧 TicketStore 的字段名
        summary: str | None = None,
        details: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """创建工单；同 idempotency_key 幂等返回。"""
        key = (idempotency_key or "").strip() or None
        with self._lock:
            if key and key in self._by_key:
                return {**self._by_key[key], "idempotent_replay": True}

            now = datetime.now().isoformat(timespec="seconds")
            ticket_id = self._new_id()
            record: dict[str, Any] = {
                "ticket_id": ticket_id,
                "title": title or summary or "",
                "summary": title or summary or "",
                "description": description or details or "",
                "details": description or details or "",
                "type": ticket_type or category or "general",
                "category": ticket_type or category or "general",
                "priority": priority,
                "status": "created",
                "user_id": user_id,
                "created_at": now,
                "updated_at": now,
                "idempotency_key": key,
                "idempotent_replay": False,
                "success": True,
            }
            self._by_id[ticket_id] = record
            if key:
                self._by_key[key] = record
            return dict(record)

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._by_id.get(ticket_id)
            return dict(rec) if rec else None

    def query_by_user(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._by_id.values() if t.get("user_id") == user_id]

    def update_status(self, ticket_id: str, status: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._by_id.get(ticket_id)
            if rec is None:
                return None
            rec["status"] = status
            rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
            return dict(rec)

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_key.clear()


# 全局默认实例（测试可替换）
_default_repo: TicketRepository | None = None


def get_default_repo() -> TicketRepository:
    global _default_repo
    if _default_repo is None:
        _default_repo = TicketRepository()
    return _default_repo


def set_default_repo(repo: TicketRepository | None) -> None:
    global _default_repo
    _default_repo = repo


__all__ = ["TicketRepository", "get_default_repo", "set_default_repo"]

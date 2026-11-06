"""跨会话用户事实（T-P2-02 四级作用域的最小可用实现）。

为什么需要它：短期记忆（Redis，30 分钟）解决"这一轮聊了什么"，
但"该用户上周提过退款工单 TK-..." 这类事实跨会话丢失，客服需要重复询问。

存储：LangGraph Store，命名空间按 org / user 隔离：
    ("org", org_id, "user", user_id, "facts")
这样多租户部署时不会串数据，同一用户跨会话可见。

写入是**确定性的**（不做 LLM 抽取）：工单号、产品名这类事实由业务流程直接落库，
避免再引入一条不可审计的抽取流水线。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_ORG = "default"
_KEY = "facts"


class _StoreLike(Protocol):  # pragma: no cover - 仅类型契约
    def get(self, namespace: tuple, key: str) -> Any: ...
    def put(self, namespace: tuple, key: str, value: dict) -> None: ...


@dataclass
class Fact:
    kind: str
    value: str
    created_at: float

    def as_text(self) -> str:
        labels = {"ticket": "工单", "product": "关注产品", "issue": "问题"}
        return f"{labels.get(self.kind, self.kind)}：{self.value}"


class ContextStore:
    """用户级事实存储（进程内 Store；生产可替换为 Postgres/Redis Store）。"""

    def __init__(self, store: _StoreLike | None = None, max_facts: int = 20) -> None:
        self._store = store
        self._max_facts = max_facts
        self._local: dict[tuple, list[dict]] = {}

    # ── 内部 ──

    def _namespace(self, user_id: str, org_id: str) -> tuple:
        return ("org", org_id or DEFAULT_ORG, "user", user_id or "anonymous", "facts")

    def _read(self, namespace: tuple) -> list[dict]:
        if self._store is not None:
            item = self._store.get(namespace, _KEY)
            value = getattr(item, "value", None) or {}
            return list(value.get("items") or [])
        return list(self._local.get(namespace, []))

    def _write(self, namespace: tuple, items: list[dict]) -> None:
        trimmed = items[-self._max_facts:]
        if self._store is not None:
            self._store.put(namespace, _KEY, {"items": trimmed})
        else:
            self._local[namespace] = trimmed

    # ── 公共接口 ──

    def put_fact(self, user_id: str, kind: str, value: str, *, org_id: str = DEFAULT_ORG) -> None:
        """写入一条事实；同 kind+value 重复写入只更新时间，不重复堆积。"""
        if not value:
            return
        namespace = self._namespace(user_id, org_id)
        items = self._read(namespace)
        now = time.time()
        for item in items:
            if item.get("kind") == kind and item.get("value") == value:
                item["created_at"] = now
                self._write(namespace, items)
                return
        items.append({"kind": kind, "value": value, "created_at": now})
        self._write(namespace, items)

    def get_facts(
        self, user_id: str, *, org_id: str = DEFAULT_ORG, limit: int = 5
    ) -> list[Fact]:
        items = self._read(self._namespace(user_id, org_id))[-max(1, limit):]
        return [
            Fact(kind=str(i.get("kind", "")), value=str(i.get("value", "")),
                 created_at=float(i.get("created_at", 0.0)))
            for i in items
        ]

    def format_for_prompt(self, user_id: str, *, org_id: str = DEFAULT_ORG, limit: int = 5) -> str:
        """给提示词用的紧凑文本；无事实时返回空串。"""
        facts = self.get_facts(user_id, org_id=org_id, limit=limit)
        if not facts:
            return ""
        return "该用户近期记录：\n" + "\n".join(f"- {fact.as_text()}" for fact in facts)

    def clear(self, user_id: str, *, org_id: str = DEFAULT_ORG) -> None:
        self._write(self._namespace(user_id, org_id), [])


__all__ = ["ContextStore", "Fact", "DEFAULT_ORG"]

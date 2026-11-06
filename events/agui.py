"""AG-UI 事件编码（T-P2-06 第二阶段）。

把内部流式事件翻译为 AG-UI（Agent User Interaction Protocol）事件子集，
便于后续对接 CopilotKit 等客户端。

⚠ 保真度说明：
本模块实现的是 AG-UI 的核心事件子集（RUN/STEP/TEXT_MESSAGE/RUN_ERROR/CUSTOM），
字段名按协议惯例（threadId / runId / messageId / delta / stepName）。
本环境无法访问官方 SDK 源码做逐字段核对，**接入真实 AG-UI 客户端前需对照
@ag-ui/core 的事件定义复核**（尤其是 STATE_DELTA 的 JSON Patch 语义）。

事件映射：
    session    → RUN_STARTED
    stage      → STEP_STARTED + STEP_FINISHED（我们的图只在节点完成时产出事件）
    token      → TEXT_MESSAGE_START(一次) + TEXT_MESSAGE_CONTENT
    response   → TEXT_MESSAGE_END + RUN_FINISHED
    审批中断   → CUSTOM{name: approval_required} + RUN_FINISHED
    error      → RUN_ERROR
"""
from __future__ import annotations

import uuid
from typing import Any


class AGUIEncoder:
    """有状态编码器：负责维护 messageId 与事件序列的先后顺序。"""

    def __init__(self, *, thread_id: str, run_id: str | None = None) -> None:
        self.thread_id = thread_id
        self.run_id = run_id or uuid.uuid4().hex
        self._message_id: str | None = None
        self._started = False
        self._finished = False

    # ── 基础事件 ──

    def _run_started(self) -> dict[str, Any]:
        return {"type": "RUN_STARTED", "threadId": self.thread_id, "runId": self.run_id}

    def _text_start(self) -> dict[str, Any]:
        if self._message_id is None:
            self._message_id = uuid.uuid4().hex
        return {
            "type": "TEXT_MESSAGE_START",
            "messageId": self._message_id,
            "role": "assistant",
        }

    def _text_content(self, delta: str) -> dict[str, Any]:
        if self._message_id is None:
            raise RuntimeError("TEXT_MESSAGE_CONTENT 之前必须先 TEXT_MESSAGE_START")
        return {"type": "TEXT_MESSAGE_CONTENT", "messageId": self._message_id, "delta": delta}

    def _text_end(self) -> dict[str, Any]:
        if self._message_id is None:
            self._message_id = uuid.uuid4().hex
        event = {"type": "TEXT_MESSAGE_END", "messageId": self._message_id}
        self._message_id = None
        return event

    def _finish(self) -> dict[str, Any]:
        self._finished = True
        return {"type": "RUN_FINISHED", "threadId": self.thread_id, "runId": self.run_id}

    # ── 主入口 ──

    def encode(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """把一条内部事件翻译成 0..N 条 AG-UI 事件。"""
        kind = event.get("type")

        if kind == "session":
            self.thread_id = event.get("session_id") or self.thread_id
            if self._started:
                return []
            self._started = True
            return [self._run_started()]

        if kind == "stage":
            node = str(event.get("node", ""))
            label = str(event.get("label", node))
            return [
                {"type": "STEP_STARTED", "stepName": node, "metadata": {"label": label}},
                {"type": "STEP_FINISHED", "stepName": node},
            ]

        if kind == "token":
            text = str(event.get("text", ""))
            if not text:
                return []
            out: list[dict[str, Any]] = []
            if self._message_id is None:
                out.append(self._text_start())
            out.append(self._text_content(text))
            return out

        if kind == "response":
            out = []
            response_text = str(event.get("response", ""))
            # 没有 token 流（例如非流式模型）时，用完整回复补一条消息
            if self._message_id is None and response_text:
                out.extend([self._text_start(), self._text_content(response_text)])
            if self._message_id is not None:
                out.append(self._text_end())
            if event.get("approval_id"):
                out.append({
                    "type": "CUSTOM",
                    "name": "approval_required",
                    "value": {
                        "approvalId": event["approval_id"],
                        "intent": event.get("intent", "unknown"),
                    },
                })
            else:
                out.append({
                    "type": "STATE_DELTA",
                    "delta": [
                        {"op": "replace", "path": "/intent", "value": event.get("intent", "unknown")},
                        {"op": "replace", "path": "/compliancePassed",
                         "value": bool(event.get("compliance_passed", True))},
                    ],
                })
            out.append(self._finish())
            return out

        if kind == "error":
            self._finished = True
            return [{
                "type": "RUN_ERROR",
                "message": str(event.get("message", "unknown error")),
                "code": event.get("status"),
            }]

        if kind == "done":
            if self._finished:
                return []
            self._finished = True
            return [self._finish()]

        # 未识别事件按原样透传（便于调试）
        return [{"type": "RAW", "event": event}]


__all__ = ["AGUIEncoder"]

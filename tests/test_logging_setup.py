"""结构化日志与 request_id 测试。"""
from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from api.main import create_app
from config import Settings
from security.approvals import MemoryApprovalStore
from tracing.logging_setup import (
    JsonFormatter,
    RequestIdFilter,
    configure_logging,
    request_id_var,
    reset_request_id,
    set_request_id,
)


class StubSTM:
    async def add_message(self, sid, role, content):
        return None

    async def get_history(self, sid):
        return []


class StubLTM:
    def __init__(self):
        self._documents = []

    def add_document(self, content, source="", metadata=None):
        return "id"


def _record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(name="test", level=logging.INFO, pathname=__file__,
                             lineno=1, msg=msg, args=(), exc_info=None)


def test_filter_injects_request_id():
    token = set_request_id("abc123")
    try:
        rec = _record()
        assert RequestIdFilter().filter(rec) is True
        assert rec.request_id == "abc123"
    finally:
        reset_request_id(token)


def test_json_formatter_outputs_valid_json():
    token = set_request_id("req-1")
    try:
        rec = _record("处理失败")
        RequestIdFilter().filter(rec)
        payload = json.loads(JsonFormatter().format(rec))
        assert payload["msg"] == "处理失败"
        assert payload["request_id"] == "req-1"
        assert payload["level"] == "INFO"
    finally:
        reset_request_id(token)


def test_configure_logging_is_idempotent():
    configure_logging("INFO", "text")
    configure_logging("DEBUG", "json")
    own = [h for h in logging.getLogger().handlers if getattr(h, "_heteroagent", False)]
    assert len(own) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_request_scoped_id_does_not_leak():
    st = Settings(require_auth=False, api_keys="", rate_limit_per_minute=1000)
    app = create_app(settings=st, graph=None, short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    client = TestClient(app)

    resp = client.get("/health/live", headers={"X-Request-ID": "trace-42"})
    assert resp.headers["X-Request-ID"] == "trace-42"
    # 请求结束后上下文应复位，避免线程复用导致串号
    assert request_id_var.get() == "-"


def test_generated_request_id_when_absent():
    st = Settings(require_auth=False, api_keys="", rate_limit_per_minute=1000)
    app = create_app(settings=st, graph=None, short_term_memory=StubSTM(),
                     long_term_memory=StubLTM(), approvals=MemoryApprovalStore())
    client = TestClient(app)
    resp = client.get("/health/live")
    assert len(resp.headers["X-Request-ID"]) >= 16

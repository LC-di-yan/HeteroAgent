"""结构化输出开关测试（代理不支持 response_format 时避免多余请求）。"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel

import agents.llm_utils as lu
from config import Settings


class _Schema(BaseModel):
    value: str = "ok"


class _CountingLLM:
    """记录结构化调用次数，用于验证开关是否真的跳过请求。"""

    def __init__(self):
        self.structured_calls = 0

    def with_structured_output(self, schema, **kwargs):
        return _Binder(self)


class _Binder:
    def __init__(self, owner):
        self.owner = owner

    async def ainvoke(self, messages, **kwargs):
        self.owner.structured_calls += 1
        return {"value": "ok"}


def test_structured_enabled_makes_call(monkeypatch):
    monkeypatch.setattr(lu, "_warned_unsupported", False)
    import config

    monkeypatch.setattr(config, "settings", Settings(structured_output_enabled=True))
    llm = _CountingLLM()
    out = asyncio.run(lu.run_structured(llm, _Schema, []))
    assert out is not None and out.value == "ok"
    assert llm.structured_calls == 1


def test_structured_disabled_skips_call_entirely(monkeypatch):
    """关闭时不应发起任何结构化请求（避免代理上翻倍延迟与成本）。"""
    import config

    monkeypatch.setattr(config, "settings", Settings(structured_output_enabled=False))
    llm = _CountingLLM()
    out = asyncio.run(lu.run_structured(llm, _Schema, []))
    assert out is None          # 交回调用方走文本兜底
    assert llm.structured_calls == 0   # 关键：没有浪费请求


def test_warn_once_on_unsupported(monkeypatch):
    """不支持结构化的代理只告警一次，避免刷屏。"""
    import config

    monkeypatch.setattr(config, "settings", Settings(structured_output_enabled=True))
    monkeypatch.setattr(lu, "_warned_unsupported", False)

    # with_structured_output 返回自身会被视为不支持，因此用独立 binder
    class _FailBinder:
        async def ainvoke(self, messages, **kwargs):
            raise RuntimeError("BadRequest")

    class _Owner:
        def with_structured_output(self, schema, **kwargs):
            return _FailBinder()

    for _ in range(3):
        assert asyncio.run(lu.run_structured(_Owner(), _Schema, [])) is None
    assert lu._warned_unsupported is True

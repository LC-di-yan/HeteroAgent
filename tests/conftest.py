"""共享测试基座：路径、环境、假 Redis、假 LLM。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试期间禁止真实网络调用与真实数据目录
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("REQUIRE_AUTH", "false")
os.environ.setdefault("OTEL_CONSOLE_EXPORT", "false")


class FakeLLM:
    """最小 ChatModel 替身：按队列返回文本，可断言调用次数。"""

    def __init__(self, responses: list[str] | None = None, structured: object | None = None):
        self.responses = list(responses or ["knowledge_rag"])
        self.calls: list[object] = []
        self._structured = structured

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        text = self.responses.pop(0) if self.responses else "knowledge_rag"

        class _Msg:
            content = text

        return _Msg()

    def with_structured_output(self, schema, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        result = await self.ainvoke(messages)
        yield result


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def tmp_index(tmp_path):
    return str(tmp_path / "faiss_index")

@pytest.fixture(autouse=True)
def _isolate_knowledge_backend():
    """隔离 tooling.server 的全局检索后端，避免测试间互相污染。"""
    try:
        from tooling.server import reset_knowledge_backend
    except Exception:  # tooling 尚未导入时无需清理
        yield
        return
    reset_knowledge_backend()
    yield
    reset_knowledge_backend()

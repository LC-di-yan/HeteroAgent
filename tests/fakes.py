"""离线测试用：支持 bind_tools 的假 ChatModel。"""
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeToolCallingModel(BaseChatModel):
    """按脚本返回 AIMessage（可带 tool_calls）；bind_tools 返回自身。"""

    responses: list[BaseMessage] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

class StructuredFakeLLM:
    """支持 with_structured_output 的假模型：按队列返回 schema 实例。

    payload 可以是 schema 实例，也可以是 dict（走 model_validate 校验）。
    """

    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list = []
        self._schema = None

    def with_structured_output(self, schema, **kwargs):  # noqa: ANN001, ANN003
        # 返回独立绑定对象（不要返回 self）：agent 侧用 "model is llm" 判定桩模型不支持结构化，
        # 返回自身会让结构化路径被跳过。
        return _StructuredBinder(self, schema)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(messages)
        payload = self.payloads.pop(0) if self.payloads else {}
        return payload


class _StructuredBinder:
    """把 StructuredFakeLLM 绑定到某个 schema，模拟 with_structured_output 的返回。"""

    def __init__(self, owner: StructuredFakeLLM, schema) -> None:
        self.owner = owner
        self.schema = schema

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.owner.calls.append(messages)
        payload = self.owner.payloads.pop(0) if self.owner.payloads else {}
        if isinstance(payload, self.schema):
            return payload
        return self.schema.model_validate(payload)

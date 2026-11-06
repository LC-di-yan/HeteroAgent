"""create_agent 工具调用 Agent 工厂（T-P1-02 / T-P1-07）。

用 LangChain 1.x 的 create_agent 把「LLM 写文本 → 正则/JSON 猜动作」
替换为真正的 tool_calls 循环；middleware 提供重试与工具错误兜底。

设计约束：
- Supervisor 仍用确定性 StateGraph 编排（金融场景可预测性优先，见 ADR）；
- 本工厂用于需要工具调用的业务 Agent（当前是工单域）；
- 模型必须支持 bind_tools，否则调用方应退回旧解析流程。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware, ToolRetryMiddleware

logger = logging.getLogger(__name__)


class ToolCallingNotSupported(RuntimeError):
    """模型不支持 bind_tools 时抛出，调用方回退旧流程。"""


def model_supports_tools(llm: Any) -> bool:
    bind = getattr(llm, "bind_tools", None)
    return callable(bind)


def build_tool_agent(
    llm: Any,
    tools: list[Callable],
    system_prompt: str,
    name: str,
    *,
    max_retries: int = 2,
    use_checkpointer: Any | None = None,
) -> Any:
    """构建带 middleware 的 create_agent 实例。

    tools: LangChain @tool 函数或 BaseTool 列表（来自 tooling/client 适配）。
    """
    if not model_supports_tools(llm):
        raise ToolCallingNotSupported(
            f"模型 {type(llm).__name__} 不支持 bind_tools，无法构建工具调用 Agent"
        )
    middleware: list[Any] = []
    if max_retries > 0:
        middleware.append(ModelRetryMiddleware(max_retries=max_retries))
        middleware.append(ToolRetryMiddleware(max_retries=max_retries))
    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        name=name,
        middleware=middleware,
        checkpointer=use_checkpointer,
    )

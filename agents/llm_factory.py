"""模型工厂与分级（T-P3-04 的可用部分）。

为什么要分级：路由、意图分类这类"轻任务"用主模型既贵又慢。
配置里早就有 LIGHT_MODEL_NAME，但之前没有任何地方使用（死配置）。

分级策略：
- 主模型：知识问答生成、合规判定（准确性优先，允许流式）
- 轻模型：Supervisor 路由（单轮判定，不流式）；未配置时自动复用主模型
"""
from __future__ import annotations

import logging
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)


def _demo_model():
    from agents.demo_mode import ScriptedChatModel

    logger.warning("DEMO_MODE 已启用：使用脚本化假模型，仅用于演示，不可用于生产")
    return ScriptedChatModel()


def build_main_llm(settings: Settings) -> Any:
    """主模型：生成与合规判定；开流式（SSE 端点需要 token 增量）。"""
    if settings.demo_mode:
        return _demo_model()

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.model_name,
        temperature=settings.temperature,
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        streaming=True,
    )


def build_light_llm(settings: Settings, *, main_llm: Any | None = None) -> Any:
    """轻模型：路由/分类；未单独配置时复用主模型（避免多建一个客户端）。"""
    if settings.demo_mode:
        return main_llm or _demo_model()

    if settings.effective_light_model == settings.model_name:
        return main_llm or build_main_llm(settings)

    from langchain_openai import ChatOpenAI

    logger.info("轻模型已启用: %s（主模型 %s）", settings.effective_light_model, settings.model_name)
    return ChatOpenAI(
        model=settings.effective_light_model,
        temperature=0.0,
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        streaming=False,
    )


__all__ = ["build_main_llm", "build_light_llm"]

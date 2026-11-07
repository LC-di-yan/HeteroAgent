"""模型工厂（兼容层）：按角色返回主模型 / 轻模型。

真正的构造逻辑在 agents/provider.py（含参数签名过滤与 Responses API 开关）。
保留本模块是为了让调用方语义清晰：谁需要"主"、谁需要"轻"。
"""
from __future__ import annotations

import logging
from typing import Any

from agents.provider import build_chat_model
from config import Settings

logger = logging.getLogger(__name__)


def build_main_llm(settings: Settings) -> Any:
    """主模型：生成与合规判定；开流式并回传 usage。"""
    return build_chat_model(settings, tier="main")


def build_light_llm(settings: Settings, *, main_llm: Any | None = None) -> Any:
    """轻模型：路由/分类；未单独配置时复用主模型（避免多建一个客户端）。"""
    if not getattr(settings, "demo_mode", False) and settings.effective_light_model == settings.model_name:
        return main_llm or build_main_llm(settings)
    return build_chat_model(settings, tier="light")


__all__ = ["build_main_llm", "build_light_llm"]

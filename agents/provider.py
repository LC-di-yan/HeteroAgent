"""模型 Provider 层（T-P1-04）。

为什么需要一层封装：
- 不同供应商/版本支持的参数不同（Responses API、reasoning_effort、prompt_cache_key），
  直接构造 ChatOpenAI 时写死参数，升级 SDK 就会在运行时炸；
- 这里按**运行时签名探测**过滤参数：支持的传下去，不支持的打日志丢弃，
  保证升级/换供应商不会因为一个未知 kwarg 直接 500。

同时修一个隐性问题：开关流式（streaming=True）后若不设 stream_usage，
流式响应不会回传 usage，成本与 token 统计会静默归零。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

Tier = Literal["main", "light"]


def _supported_fields(model_cls: Any) -> set[str]:
    """字段名 + pydantic 别名。

    注意：ChatOpenAI 的 api_key / base_url / timeout 都是**别名**
    （真实字段名是 openai_api_key / openai_api_base / request_timeout），
    只比较 model_fields 的键会把凭据当成未知参数丢掉。
    """
    fields = getattr(model_cls, "model_fields", {}) or {}
    names: set[str] = set(fields)
    for info in fields.values():
        alias = getattr(info, "alias", None)
        if isinstance(alias, str):
            names.add(alias)
        validation_alias = getattr(info, "validation_alias", None)
        if isinstance(validation_alias, str):
            names.add(validation_alias)
    return names


def filter_supported_kwargs(model_cls: Any, kwargs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按目标类的字段过滤参数，返回 (可用参数, 被丢弃的键)。"""
    supported = _supported_fields(model_cls)
    usable: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in kwargs.items():
        if value is None or value == "":
            continue
        if supported and key not in supported:
            dropped.append(key)
            continue
        usable[key] = value
    return usable, dropped


@dataclass
class ProviderSpec:
    model: str
    temperature: float
    streaming: bool
    max_retries: int
    timeout: float
    use_responses_api: bool = False
    reasoning_effort: str = ""
    prompt_cache_key: str = ""
    stream_usage: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_tier(cls, settings: Any, tier: Tier) -> ProviderSpec:
        is_main = tier == "main"
        model = settings.model_name if is_main else settings.effective_light_model
        return cls(
            model=model,
            temperature=settings.temperature if is_main else 0.0,
            streaming=is_main,  # 路由不需要流式
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_seconds,
            use_responses_api=bool(getattr(settings, "use_responses_api", False)),
            reasoning_effort=str(getattr(settings, "reasoning_effort", "") or ""),
            prompt_cache_key=str(getattr(settings, "prompt_cache_key", "") or ""),
            stream_usage=bool(getattr(settings, "stream_usage", True)),
        )


def build_chat_model(settings: Any, *, tier: Tier = "main") -> Any:
    """构造 ChatOpenAI（按 tier 决定模型与流式），参数经签名过滤。"""
    if getattr(settings, "demo_mode", False):
        from agents.demo_mode import ScriptedChatModel

        logger.warning("DEMO_MODE 已启用：使用脚本化假模型，仅用于演示，不可用于生产")
        return ScriptedChatModel()

    from langchain_openai import ChatOpenAI

    spec = ProviderSpec.for_tier(settings, tier)
    api_key = getattr(settings, "openai_api_key", "") or None
    base_url = getattr(settings, "openai_base_url", "") or None
    if tier == "light":
        # 路由/分类可以单独指向更小更快的模型供应商（例如决策模型）
        api_key = getattr(settings, "light_api_key", "") or api_key
        base_url = getattr(settings, "light_base_url", "") or base_url
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "temperature": spec.temperature,
        "api_key": api_key,
        "base_url": base_url,
        "timeout": spec.timeout,
        "max_retries": spec.max_retries,
        "streaming": spec.streaming,
        "stream_usage": spec.stream_usage,
        "use_responses_api": spec.use_responses_api,
        "reasoning_effort": spec.reasoning_effort or None,
    }
    if spec.prompt_cache_key:
        # 让相同前缀的请求落到同一缓存槽（供应商支持时生效；不支持的会被过滤）
        kwargs["model_kwargs"] = {"prompt_cache_key": spec.prompt_cache_key}
    kwargs.update(spec.extra)

    usable, dropped = filter_supported_kwargs(ChatOpenAI, kwargs)
    if dropped:
        logger.info("当前 langchain-openai 不支持这些参数，已忽略: %s", dropped)
    if spec.use_responses_api and "use_responses_api" in usable:
        logger.info("已启用 Responses API（模型=%s）", spec.model)
    return ChatOpenAI(**usable)


__all__ = ["ProviderSpec", "build_chat_model", "filter_supported_kwargs"]

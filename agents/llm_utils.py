"""LLM 调用辅助：结构化输出优先，文本解析兜底（T-P1-03）。"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


async def run_structured[SchemaT: BaseModel](
    llm: Any,
    schema: type[SchemaT],
    messages: list[Any],
    *,
    label: str = "",
) -> SchemaT | None:
    """尝试用 with_structured_output 获取 schema 实例。

    返回 None 表示"该模型/该次调用不支持结构化"，调用方应走文本解析兜底；
    结构化调用本身抛错时同样返回 None（不吞掉业务异常，只降级）。
    """
    factory = getattr(llm, "with_structured_output", None)
    if not callable(factory):
        return None
    try:
        model = factory(schema)
    except (AttributeError, NotImplementedError, TypeError) as exc:
        logger.debug("模型不支持结构化输出(%s): %s", label or schema.__name__, exc)
        return None
    if model is None:
        return None
    # 个别桩模型（测试替身）用"返回自身"表示不支持结构化；此时不能调用，
    # 否则会白白消耗一次模型调用，导致兜底路径拿到错位的响应。
    if model is llm:
        logger.debug("模型 with_structured_output 返回自身(%s)，视为不支持", label or schema.__name__)
        return None
    try:
        out = await model.ainvoke(messages)
    except Exception as exc:  # 结构化调用失败 → 交回调用方兜底
        logger.warning("结构化调用失败(%s): %s", label or schema.__name__, type(exc).__name__)
        return None

    if isinstance(out, schema):
        return out
    if isinstance(out, dict):
        try:
            return schema.model_validate(out)
        except Exception as exc:
            logger.warning("结构化输出校验失败(%s): %s", label or schema.__name__, exc)
            return None
    return None

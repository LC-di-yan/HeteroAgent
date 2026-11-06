"""日志配置：结构化输出 + request_id 贯穿（可观测性补完）。

问题：线上排查时，若日志里没有 request_id，就无法把"一次用户请求触发的
路由 / 检索 / 合规 / 工具调用"串起来；纯文本日志也难以被日志系统索引。

做法：
- contextvar 保存 request_id，中间件在请求开始时写入、结束时清理；
- logging.Filter 把它注入每条 LogRecord；
- 可选 JSON 输出（LOG_FORMAT=json），字段固定，便于采集。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_TEXT_FORMAT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RequestIdFilter(logging.Filter):
    """把当前请求的 request_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON：时间、级别、logger、消息、request_id、异常栈。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def set_request_id(request_id: str):
    """写入当前上下文，返回 token 供 reset 使用。"""
    return request_id_var.set(request_id or "-")


def reset_request_id(token) -> None:  # noqa: ANN001
    try:
        request_id_var.reset(token)
    except ValueError:  # 跨上下文 reset（如后台任务）时忽略
        request_id_var.set("-")


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """配置根 logger；重复调用是幂等的（移除自己加的 handler）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_heteroagent", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler._heteroagent = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt=_DATE_FORMAT))

    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))


__all__ = [
    "RequestIdFilter",
    "JsonFormatter",
    "configure_logging",
    "request_id_var",
    "reset_request_id",
    "set_request_id",
]

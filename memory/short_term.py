"""
短期记忆 — 基于 Redis 的会话级记忆（G13 修订版）。

- get_context_window 改用 tiktoken（cl100k_base）精确计数，懒加载；
  编码器下载/加载失败时降级为 len(text)//2 估算，并且只 logger.warning 一次（G13）。
- 其余接口（add_message / get_history / clear）保持不变。
- Redis 不可用时降级为进程内内存分支，并对会话数设上限（默认 1000，LRU 淘汰）；
  Redis 连接失败只尝试一次，避免每次读写都重试连接。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import datetime
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - 无 redis 时走内存分支
    aioredis = None

try:
    import tiktoken
except ImportError:  # pragma: no cover - 无 tiktoken 时走 len//2 估算
    tiktoken = None

logger = logging.getLogger(__name__)

_FALLBACK_TOKEN_DIVISOR = 2  # 降级估算：len(text) // 2（与旧行为一致）
_TOKEN_ENCODER: Any = None
_TOKEN_ENCODER_READY = False


def _get_token_encoder():
    """懒加载 tiktoken 编码器；失败只告警一次，之后固定走估算分支（G13）。"""
    global _TOKEN_ENCODER, _TOKEN_ENCODER_READY
    if _TOKEN_ENCODER_READY:
        return _TOKEN_ENCODER
    _TOKEN_ENCODER_READY = True
    if tiktoken is None:
        logger.warning(
            "tiktoken 未安装，token 计数降级为 len//2 估算（G13）；如需精确计数请安装 tiktoken"
        )
        return None
    try:
        _TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # 离线且无本地缓存时下载会失败
        _TOKEN_ENCODER = None
        logger.warning(
            "tiktoken cl100k_base 加载失败，token 计数降级为 len//2 估算（G13）: %s", exc
        )
    return _TOKEN_ENCODER


def count_tokens(text: str) -> int:
    """token 计数：tiktoken 精确计数；编码器不可用时降级 len//2（G13）。"""
    if not text:
        return 0
    encoder = _get_token_encoder()
    if encoder is None:
        return len(text) // _FALLBACK_TOKEN_DIVISOR
    return len(encoder.encode(text))


class ShortTermMemory:
    """短期记忆：基于 Redis 的会话缓存。

    特点：
    - Redis 存储，支持分布式部署；TTL 自动过期（默认 30 分钟）
    - 保留最近 N 轮对话（ltrim 滑动窗口）
    - Redis 不可用时降级为进程内有序字典（会话数上限 + LRU 淘汰）
    """

    DEFAULT_MAX_SESSIONS = 1000

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        redis_client: Any = None,
    ):
        self.max_turns = max(int(max_turns), 1)
        self.ttl_seconds = int(ttl_seconds)
        self.max_sessions = max(int(max_sessions), 1)
        self._redis_url = redis_url
        self._redis: Any = redis_client  # 允许测试直接注入 fakeredis 客户端
        self._redis_broken = False
        self._fallback_store: OrderedDict[str, list[dict]] = OrderedDict()

    async def _get_redis(self):
        """懒加载 Redis 连接；失败只尝试一次，之后固定走内存分支。"""
        if self._redis is not None:
            return self._redis
        if self._redis_broken or aioredis is None:
            return None
        try:
            client = aioredis.from_url(self._redis_url, decode_responses=True)
            await client.ping()
        except Exception as exc:
            self._redis_broken = True
            logger.warning(
                "Redis 不可用（%s），短期记忆降级为进程内内存分支（最多 %d 个会话，LRU 淘汰）",
                exc,
                self.max_sessions,
            )
            return None
        self._redis = client
        return self._redis

    def _session_key(self, session_id: str) -> str:
        return "smartcs:short_term:" + str(session_id)

    def _fallback_touch(self, session_id: str) -> None:
        """LRU：把会话标记为最近使用；新增会话时按上限淘汰最旧会话。"""
        if session_id in self._fallback_store:
            self._fallback_store.move_to_end(session_id)
            return
        self._fallback_store[session_id] = []
        while len(self._fallback_store) > self.max_sessions:
            evicted, _ = self._fallback_store.popitem(last=False)
            logger.debug("内存分支会话数超限，LRU 淘汰: %s", evicted)

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """添加一条对话消息（Redis 分支会刷新 TTL 并裁剪到最近 max_turns 条）。"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }

        client = await self._get_redis()

        if client is not None:
            key = self._session_key(session_id)
            await client.rpush(key, json.dumps(message, ensure_ascii=False))
            await client.ltrim(key, -self.max_turns, -1)
            await client.expire(key, self.ttl_seconds)
            return

        self._fallback_touch(session_id)
        self._fallback_store[session_id].append(message)
        if len(self._fallback_store[session_id]) > self.max_turns:
            self._fallback_store[session_id] = self._fallback_store[session_id][-self.max_turns:]

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict]:
        """获取对话历史（最近 last_n 条，默认 max_turns 条）。"""
        client = await self._get_redis()

        if client is not None:
            key = self._session_key(session_id)
            n = last_n or self.max_turns
            raw = await client.lrange(key, -n, -1)
            return [json.loads(item) for item in raw]

        if session_id in self._fallback_store:
            self._fallback_store.move_to_end(session_id)  # 读取也刷新 LRU 顺序
        history = self._fallback_store.get(session_id, [])
        if last_n:
            return list(history[-last_n:])
        return list(history)

    async def clear(self, session_id: str) -> None:
        """清除指定 session 的短期记忆。"""
        client = await self._get_redis()
        if client is not None:
            await client.delete(self._session_key(session_id))
        else:
            self._fallback_store.pop(session_id, None)

    async def get_context_window(self, session_id: str, max_tokens: int = 4000) -> str:
        """获取适配上下文窗口大小的对话历史文本（G13：tiktoken 计数）。

        从最近的消息向前回溯，累计 token 数不超过 max_tokens，返回文本按时间正序。
        """
        history = await self.get_history(session_id)

        context_parts: list[str] = []
        estimated_tokens = 0

        for message in reversed(history):
            message_text = str(message.get("role", "user")) + ": " + str(message.get("content", ""))
            message_tokens = count_tokens(message_text)
            if estimated_tokens + message_tokens > max_tokens:
                break
            context_parts.insert(0, message_text)
            estimated_tokens += message_tokens

        return "\n".join(context_parts)

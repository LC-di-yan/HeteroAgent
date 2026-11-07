"""API 版重排器（/rerank 端点）。

本地 sentence-transformers 需要下载模型且 CPU 推理会占用进程；
若供应商提供 /rerank（如 bge-reranker-v2-m3），直接调 API 更省事也更快。

返回契约与本地重排一致：按相关性降序的文档列表，每项附 rerank_score。
失败时返回 None，由调用方决定降级（保持原序）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def _post_rerank(
    base_url: str,
    api_key: str,
    model: str,
    query: str,
    documents: list[str],
    top_n: int,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "query": query,
        "documents": documents,
        "top_n": max(1, int(top_n)),
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/rerank",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ApiReranker:
    """通过 /rerank 端点做重排（异步包装，不阻塞事件循环）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "bge-reranker-v2-m3",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def rerank(
        self, query: str, documents: list[dict], top_k: int = 3
    ) -> list[dict] | None:
        if not documents:
            return []
        texts = [str(d.get("content", ""))[:2000] for d in documents]
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(
                    _post_rerank, self.base_url, self.api_key, self.model,
                    query, texts, top_k, self.timeout,
                ),
                timeout=self.timeout + 5,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning("API 重排失败，保持原序: %s", type(exc).__name__)
            return None
        except Exception as exc:  # noqa: BLE001 - 重排失败不应影响回答
            logger.warning("API 重排异常，保持原序: %s", type(exc).__name__)
            return None

        results = data.get("results") or []
        if not results:
            logger.warning("API 重排返回空结果，保持原序")
            return None

        out: list[dict] = []
        for item in results:
            idx = item.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(documents):
                continue
            enriched = dict(documents[idx])
            score = item.get("relevance_score", item.get("score", 0.0))
            try:
                enriched["rerank_score"] = float(score)
            except (TypeError, ValueError):
                enriched["rerank_score"] = 0.0
            out.append(enriched)
        return out[:top_k] if out else None


def build_api_reranker(settings: Any) -> ApiReranker | None:
    """按配置构造；未配置端点/凭据时返回 None（调用方回退本地或跳过）。"""
    base = str(getattr(settings, "rerank_api_base_url", "") or "").strip()
    key = str(getattr(settings, "rerank_api_key", "") or "").strip()
    if not base or not key:
        return None
    return ApiReranker(
        base_url=base,
        api_key=key,
        model=str(getattr(settings, "rerank_api_model", "bge-reranker-v2-m3")),
        timeout=float(getattr(settings, "rerank_timeout_seconds", 15.0)),
    )


__all__ = ["ApiReranker", "build_api_reranker"]

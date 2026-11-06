"""
长期记忆 — 向量检索（FAISS / Milvus）+ BM25 关键词检索的混合检索层。

本次重写针对评估报告中的 G7 / G8 / T-P0-10：

1. 彻底删除 hash 伪向量（G7）
   旧实现用 sha256 -> np.random.seed -> randn 生成随机数冒充语义向量，
   检索看似有分数、实为噪声。现在任何 Embedding 不可用的路径（未配置 /
   调用失败 / 维度不符 / 范数非法）统一抛出 EmbeddingUnavailableError；
   settings.allow_embedding_fallback 为 True 时只是额外记录一条明确的
   warning 日志，依然抛出异常，绝不伪造向量。

2. 索引与元数据严格对齐（G8）
   - doc_id = md5(content)，同内容重复 add 直接返回旧 id，不重复入索引；
   - 先算向量再改状态，Embedding 失败不会污染 _documents / _index；
   - load() 校验 index.ntotal == len(documents) 与维度，加载失败时同时清空
     _documents，杜绝「索引 N 条向量 / 元数据 M 条文档」的错位；
   - save() 同时写 faiss 索引与 meta.json，先写临时文件再 os.replace 原子替换。

3. 异步边界（T-P0-10）
   asearch() 为生产主路径：Embedding 走 AsyncOpenAI（不阻塞事件循环），
   FAISS / BM25 / RRF 等 CPU 计算放进 asyncio.to_thread()。

4. 单一实现，禁止复制粘贴
   分词、BM25、RRF、Embedding 全部收敛到模块级 tokenize / BM25Index /
   rrf_fuse 以及 EmbeddingMixin / HybridSearchMixin，FAISS 与 Milvus 后端共享。

5. 返回契约（供 knowledge_rag 置信度阈值使用）
   每条结果同时带两种分数：
   - score ∈ [0,1]：向量通道为 cosine 裁剪到 [0,1]，BM25-only 命中为该路分数的
     min-max 归一化；可直接与 0.30 这类阈值比较。
   - rrf_score：原始 RRF 融合分（约 0.016~0.033），只用于排序，绝不用于阈值判断。

BM25 性能：文档入库时预分词并缓存 doc_id -> tokens/tf/dl，IDF 只在语料变更时
（脏标记）重建一次，查询阶段不做全库重分词。RRF 融合 k=60 保持不变。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import numpy as np

try:
    import faiss
except ImportError:  # pragma: no cover - faiss 缺失时仍可运行（仅 BM25）
    faiss = None

try:
    import openai
except ImportError:  # pragma: no cover - openai 缺失时 Embedding 显式失败
    openai = None

try:
    from config import Settings
except ImportError:  # pragma: no cover - 允许脱离 config 使用（需显式传 settings）
    Settings = None

logger = logging.getLogger(__name__)

__all__ = [
    "BM25Index",
    "EmbeddingMixin",
    "EmbeddingUnavailableError",
    "HybridSearchMixin",
    "LongTermMemory",
    "MilvusLongTermMemory",
    "create_vector_store",
    "rrf_fuse",
    "to_unit_score",
    "tokenize",
]

EMBEDDING_CACHE_LIMIT = 4096
RRF_K_DEFAULT = 60


class EmbeddingUnavailableError(RuntimeError):
    """Embedding 不可用（未配置 / 调用失败 / 维度或范数非法）。

    G7：这是唯一的降级方式，系统不会生成随机伪向量继续往下走。
    """


# ===============================================================
#  共享检索原语：tokenize / BM25Index / rrf_fuse（唯一实现）
# ===============================================================


def tokenize(text: str) -> list[str]:
    """中英文混合分词（无第三方分词器依赖）。

    - 英文/数字：连续串作为一个 token；
    - 中文：按 **双字 bigram** 切分（"退款流程" → 退款/款流/流程），单字词保留单字。
      bigram 比逐字切分更能表达词边界（"退" 不再和 "退休" 混淆），
      同时避免引入 jieba 之类的重依赖。
    """
    if not text:
        return []
    normalized = str(text).lower()
    tokens: list[str] = re.findall(r"[a-z0-9]+", normalized)
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        tokens.extend(run)  # 单字：保证召回（"退" 也能命中）
        if len(run) > 1:  # 双字：提供词边界信号（"退款" 比 单字 更具体）
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def document_index_text(doc: dict) -> str:
    """写入 BM25 / Embedding 的文本：若有 contextual retrieval 的前缀则拼上。"""
    context = str((doc.get("metadata") or {}).get("context") or "").strip()
    content = str(doc.get("content", "") or "")
    return context + "\n" + content if context else content


def rrf_fuse(
    vector_results: list[dict],
    bm25_results: list[dict],
    k: int = RRF_K_DEFAULT,
    top_k: int = 5,
) -> list[dict]:
    """Reciprocal Rank Fusion: score(d) = sum(1 / (k + rank_i(d)))。

    只依赖排名，天然规避余弦相似度与 BM25 分数量纲不一致的问题。
    输出的 rrf_score 保留原始值（约 0.016~0.033），仅用于排序；
    与置信度阈值比较请使用 score 字段（归一化到 [0,1]）。
    """
    rrf_scores: dict[str, float] = {}
    doc_map: OrderedDict = OrderedDict()

    for results in (vector_results, bm25_results):
        for rank, doc in enumerate(results, start=1):
            doc_id = str(doc.get("id"))
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in doc_map:
                doc_map[doc_id] = doc

    seen_order = list(doc_map)
    ordered = sorted(rrf_scores, key=lambda did: (-rrf_scores[did], seen_order.index(did)))
    fused: list[dict] = []
    for doc_id in ordered[: max(int(top_k), 0)]:
        doc = dict(doc_map[doc_id])
        doc["id"] = doc_id
        doc["rrf_score"] = float(rrf_scores[doc_id])
        fused.append(doc)
    return fused


def to_unit_score(value: Any) -> float:
    """把任意相似度分数裁剪/转换为 [0, 1]（NaN / Inf / 非法值 -> 0.0）。

    这是返回契约的一部分：knowledge_rag 的置信度阈值（默认 0.30）只认这个字段。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return float(min(max(number, 0.0), 1.0))


class BM25Index:
    """增量式 BM25 索引（k1=1.5, b=0.75）。

    - 入库时一次性分词并缓存 doc_id -> tokens / tf / dl；
    - IDF 与 avgdl 只在语料变更（脏标记）后重建一次，重建只读缓存、不再分词；
    - 查询阶段只对 query 分词一次，对文档做 O(N) 的 tf 查表（与文档数线性）。
    """

    K1 = 1.5
    B = 0.75

    def __init__(self) -> None:
        self._order: list[str] = []
        self._tokens: dict[str, list[str]] = {}
        self._tf: dict[str, dict[str, int]] = {}
        self._dl: dict[str, int] = {}
        self._total_len = 0
        self._idf: dict[str, float] = {}
        self._avgdl = 0.0
        self._dirty = False

    # ---- 构建 ----

    def add(self, doc_id: str, content: str) -> None:
        tokens = tokenize(content)  # 入库时预分词，查询时不再重复分词
        tf: dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        key = str(doc_id)
        self._order.append(key)
        self._tokens[key] = tokens
        self._tf[key] = tf
        self._dl[key] = len(tokens)
        self._total_len += len(tokens)
        self._dirty = True  # 语料变更 -> IDF 需要重建

    def contains(self, doc_id: str) -> bool:
        return str(doc_id) in self._tf

    def __len__(self) -> int:
        return len(self._order)

    @property
    def dirty(self) -> bool:
        """IDF 统计是否已过期（有新文档尚未计入）。"""
        return self._dirty

    # ---- 统计重建 ----

    def rebuild_if_dirty(self) -> None:
        if not self._dirty:
            return
        n = len(self._order)
        if n == 0:
            self._idf = {}
            self._avgdl = 0.0
            self._dirty = False
            return
        df: dict[str, int] = {}
        for doc_id in self._order:
            for term in self._tf[doc_id]:
                df[term] = df.get(term, 0) + 1
        self._idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
            for term, freq in df.items()
        }
        self._avgdl = self._total_len / n
        self._dirty = False

    def idf_of(self, term: str) -> float | None:
        """返回某 token 的当前 IDF（先按需重建统计）。供测试与调试使用。"""
        self.rebuild_if_dirty()
        return self._idf.get(term)

    # ---- 查询 ----

    def search(self, query: str, top_k: int, doc_lookup: dict) -> list[dict]:
        self.rebuild_if_dirty()
        query_terms = tokenize(query)  # 唯一一次分词：只针对 query
        if not query_terms or not self._order:
            return []
        k1, b = self.K1, self.B
        avgdl = max(self._avgdl, 1e-6)
        scored: list[tuple[float, int, str]] = []
        for position, doc_id in enumerate(self._order):
            tf = self._tf[doc_id]
            dl = self._dl[doc_id]
            score = 0.0
            for term in query_terms:
                freq = tf.get(term)
                if not freq:
                    continue
                idf = self._idf.get(term)
                if idf is None:
                    continue
                score += idf * (freq * (k1 + 1.0)) / (freq + k1 * (1.0 - b + b * dl / avgdl))
            if score > 0.0:
                scored.append((score, position, doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return []

        # min-max 归一化到 [0,1]：BM25 原始分数无上界，必须与向量余弦同量纲，
        # 否则 knowledge_rag 的 0.30 置信度阈值会永远失效或永远转人工。
        raw_scores = [entry[0] for entry in scored]
        low, high = min(raw_scores), max(raw_scores)
        span = high - low

        results: list[dict] = []
        for raw_score, _position, doc_id in scored[: max(int(top_k), 0)]:
            doc = doc_lookup.get(doc_id)
            if doc is None:
                continue
            item = dict(doc)
            item["score"] = 1.0 if span <= 1e-12 else float((raw_score - low) / span)
            item["bm25_score"] = float(raw_score)  # 原始 BM25 分（调试/排序用）
            results.append(item)
        return results


# ===============================================================
#  Embedding 层（G7 核心）：真实模型 + 有界 LRU 缓存 + 显式失败
# ===============================================================


class EmbeddingMixin:
    """共享 Embedding：同步 OpenAI + 异步 AsyncOpenAI 双通道。

    优先级：注入的 embedding_fn > 显式注入的客户端 > settings 构造的 OpenAI 客户端。

    embedding_fn 仅供测试注入确定性向量（生产禁用）；生产路径必须走真实模型，
    失败即抛 EmbeddingUnavailableError，绝不产出伪向量。缓存为有界 LRU
    （OrderedDict，上限 4096 条）。
    """

    _EMBEDDING_CACHE_LIMIT = EMBEDDING_CACHE_LIMIT

    def _init_embedding(
        self,
        *,
        embedding_fn: Callable[[str], list[float]] | None = None,
        settings: Any = None,
    ) -> None:
        if settings is None:
            if Settings is None:
                raise RuntimeError("无法导入 config.Settings，请显式传入 settings")
            settings = Settings()
        self.settings = settings
        self._embedding_fn: Callable[[str], list[float]] | None = embedding_fn
        if embedding_fn is not None:
            logger.warning(
                "检测到注入的 embedding_fn（仅测试用途，生产禁用）：向量将由该函数产生，"
                "不会调用真实 Embedding 服务"
            )
        self._embedding_cache: OrderedDict = OrderedDict()  # text_md5 -> 归一化向量
        self._embedding_lock = threading.Lock()
        self._sync_client: Any = None
        self._async_client: Any = None
        if embedding_fn is None:
            self._init_openai_clients()

    def _init_openai_clients(self) -> None:
        """构造 OpenAI 同步 + 异步 Embedding 客户端（只构造，不做网络探测）。"""
        api_key = str(getattr(self.settings, "openai_api_key", "") or "")
        if openai is None or not api_key:
            logger.info(
                "未配置 Embedding API key（或 openai 未安装）：需要向量时会抛出 "
                "EmbeddingUnavailableError（G7，禁止伪向量）"
            )
            return
        timeout = float(getattr(self.settings, "embedding_timeout_seconds", 10.0) or 10.0)
        base_url = getattr(self.settings, "openai_base_url", "") or None
        try:
            self._sync_client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
            self._async_client = openai.AsyncOpenAI(
                api_key=api_key, base_url=base_url, timeout=timeout
            )
            logger.info(
                "OpenAI Embedding 客户端就绪: model=%s dim=%s timeout=%ss",
                getattr(self.settings, "embedding_model", "?"),
                getattr(self, "embedding_dim", "?"),
                timeout,
            )
        except Exception as exc:  # 构造失败同样视为不可用（不伪造向量）
            self._sync_client = None
            self._async_client = None
            logger.warning("OpenAI Embedding 客户端初始化失败: %s", exc)

    def set_embedding_client(
        self,
        *,
        sync_client: Any = None,
        async_client: Any = None,
    ) -> None:
        """测试/运维注入：替换底层 Embedding 客户端（传 None 的项保持不变）。"""
        if sync_client is not None:
            self._sync_client = sync_client
        if async_client is not None:
            self._async_client = async_client

    # ---- 缓存 ----

    @staticmethod
    def _cache_key(text: str) -> str:
        return hashlib.md5(str(text).encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Any:
        with self._embedding_lock:
            vector = self._embedding_cache.get(key)
            if vector is not None:
                self._embedding_cache.move_to_end(key)
            return vector

    def _cache_put(self, key: str, vector: Any) -> None:
        with self._embedding_lock:
            self._embedding_cache[key] = vector
            self._embedding_cache.move_to_end(key)
            while len(self._embedding_cache) > self._EMBEDDING_CACHE_LIMIT:
                self._embedding_cache.popitem(last=False)

    # ---- 失败策略（G7）----

    def _embedding_fail(self, text: str, reason: str) -> NoReturn:
        preview = str(text)[:60]
        if bool(getattr(self.settings, "allow_embedding_fallback", False)):
            logger.warning(
                "Embedding 不可用（%s）。settings.allow_embedding_fallback=True 也不会"
                "生成 hash/随机伪向量（G7），本次直接抛出 EmbeddingUnavailableError；"
                "文本预览=%r",
                reason,
                preview,
            )
        raise EmbeddingUnavailableError(
            "Embedding 不可用：" + reason
            + "。已禁止伪向量降级（G7），请配置真实 Embedding 服务。"
            + "（文本预览: " + repr(preview) + "）"
        )

    def _no_provider_reason(self) -> str:
        return (
            "未注入 embedding_fn、settings.openai_api_key 为空，且没有可用的 "
            "OpenAI Embedding 客户端"
        )

    def _finalize_vector(self, text: str, raw: Any, key: str) -> Any:
        """维度/范数校验 -> L2 归一化 -> 写入 LRU 缓存。非法输入按不可用处理。"""
        try:
            arr = np.asarray(raw, dtype=np.float32).reshape(-1)
        except Exception as exc:
            self._embedding_fail(text, "embedding 返回值无法转换为 float32 向量: " + repr(exc))
        if arr.size != int(self.embedding_dim):
            self._embedding_fail(
                text,
                "维度不符：settings.embedding_dim="
                + str(int(self.embedding_dim))
                + "，实际返回 "
                + str(int(arr.size))
                + " 维",
            )
        norm = float(np.linalg.norm(arr))
        if not math.isfinite(norm) or norm <= 0.0:
            self._embedding_fail(text, "向量范数非法（0 / NaN / Inf）")
        unit = (arr / norm).astype(np.float32)
        self._cache_put(key, unit)
        return unit

    # ---- 同步入口 ----

    def _embed_sync(self, text: str) -> Any:
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        if self._embedding_fn is not None:
            try:
                raw = self._embedding_fn(text)
            except EmbeddingUnavailableError:
                raise
            except Exception as exc:
                self._embedding_fail(text, "注入的 embedding_fn 抛出异常: " + repr(exc))
            return self._finalize_vector(text, raw, key)

        if self._sync_client is None:
            self._embedding_fail(text, self._no_provider_reason())

        try:
            response = self._sync_client.embeddings.create(
                model=self.settings.embedding_model, input=[text]
            )
            raw = response.data[0].embedding
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:
            self._embedding_fail(text, "OpenAI embeddings 同步调用失败: " + repr(exc))
        return self._finalize_vector(text, raw, key)

    # ---- 异步入口（T-P0-10）----

    async def aembed(self, text: str) -> Any:
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        if self._embedding_fn is not None:
            try:
                raw = self._embedding_fn(text)
                if inspect.isawaitable(raw):
                    raw = await raw
            except EmbeddingUnavailableError:
                raise
            except Exception as exc:
                self._embedding_fail(text, "注入的 embedding_fn 异步调用失败: " + repr(exc))
            return self._finalize_vector(text, raw, key)

        if self._async_client is None:
            self._embedding_fail(text, self._no_provider_reason())

        try:
            response = await self._async_client.embeddings.create(
                model=self.settings.embedding_model, input=[text]
            )
            raw = response.data[0].embedding
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:
            self._embedding_fail(text, "AsyncOpenAI embeddings 异步调用失败: " + repr(exc))
        return self._finalize_vector(text, raw, key)


# ===============================================================
#  混合检索编排（唯一实现，FAISS / Milvus 共用）
# ===============================================================


class HybridSearchMixin:
    """向量检索 + BM25 -> RRF 融合的统一编排，后端只需实现 _vector_search。"""

    RRF_K = RRF_K_DEFAULT

    # 由具体后端初始化：_documents(list) / _doc_by_id(dict) / _bm25(BM25Index)

    # ---- 兼容旧调用点的别名（转发到唯一实现，不是第二份逻辑）----

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return tokenize(text)

    @staticmethod
    def _rrf_fuse(
        vector_results: list[dict],
        bm25_results: list[dict],
        k: int = RRF_K_DEFAULT,
        top_k: int = 5,
    ) -> list[dict]:
        return rrf_fuse(vector_results, bm25_results, k=k, top_k=top_k)

    def _bm25_search(self, query: str, top_k: int = 5) -> list[dict]:
        return self._bm25.search(query, top_k, self._doc_by_id)

    # ---- 融合 ----

    def _merge_results(
        self,
        vector_results: list[dict],
        bm25_results: list[dict],
        query: str,
        top_k: int,
    ) -> list[dict]:
        if vector_results or bm25_results:
            results = rrf_fuse(vector_results, bm25_results, k=self.RRF_K, top_k=top_k)
        else:
            results = self._fallback_search(query, top_k)
        # 统一返回契约：
        #   score     ∈ [0,1] 可比相似度（向量通道 = cosine 裁剪；BM25-only = 该路 min-max）
        #   rrf_score = 原始 RRF 融合分（约 0.016~0.033），仅用于排序，不要拿去比阈值
        for item in results:
            item["score"] = to_unit_score(item.get("score"))
            item["rrf_score"] = float(item.get("rrf_score", 0.0) or 0.0)
        return results

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """两路都无命中时的宽松子串回退（返回字段与主路径一致）。"""
        terms = [t for t in re.split(r"\s+", str(query).strip().lower()) if t]
        if not terms or not self._documents:
            return []
        scored: list[tuple[float, int, dict]] = []
        for position, doc in enumerate(self._documents):
            blob = str(doc.get("content", "")).lower()
            score = float(sum(1 for term in terms if term in blob)) / float(len(terms))
            if score > 0:
                item = dict(doc)
                item["score"] = score
                scored.append((score, position, item))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [entry[2] for entry in scored[: max(int(top_k), 0)]]

    # ---- 公共检索入口 ----

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Hybrid Search：向量 + BM25 -> RRF(k=60)。返回契约见下。

        - score：归一化相似度 ∈ [0,1]。向量通道 = cosine 裁剪到 [0,1]；
          BM25-only 命中 = 该路分数 min-max 归一化（单条命中记 1.0）。
          该字段可直接与 knowledge_rag 的置信度阈值（默认 0.30）比较。
        - rrf_score：原始 RRF 融合分（约 0.016~0.033），只用于排序，不参与阈值判断。
        - 另有 id / content / source；BM25 通道命中还带原始 bm25_score。

        Embedding 不可用时抛 EmbeddingUnavailableError（G7），不会退化成随机向量检索。
        """
        if not self._documents:
            return []
        vector_results = self._vector_search(query, top_k=top_k * 2)
        bm25_results = self._bm25_search(query, top_k=top_k * 2)
        return self._merge_results(vector_results, bm25_results, query, top_k)

    # ---- 知识库导入 ----

    def load_knowledge_base(self, kb_dir: str) -> int:
        """从目录批量导入 **/*.txt，返回新增文档数（重复内容被去重不计）。"""
        kb_path = Path(kb_dir)
        if not kb_path.exists() or not kb_path.is_dir():
            return 0
        before = len(self._documents)
        for file_path in sorted(kb_path.rglob("*.txt")):
            try:
                text = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("读取知识库文件失败 %s: %s", file_path, exc)
                continue
            for chunk in self._chunk_text(text):
                self.add_document(
                    content=chunk, source=file_path.name, metadata={"file": str(file_path)}
                )
        return len(self._documents) - before

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        """文本分块：优先按段落聚合，超长段落按句子切分，块间保留重叠窗口。"""
        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(current_chunk) + len(para) <= chunk_size:
                current_chunk += para + "\n\n"
            elif current_chunk:
                chunks.append(current_chunk.strip())
                overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                current_chunk = overlap_text + para + "\n\n"
            else:
                sentences = para.replace("。", "。\n").replace(".", ".\n").split("\n")
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence:
                        continue
                    if len(current_chunk) + len(sentence) <= chunk_size:
                        current_chunk += sentence
                    else:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = sentence

        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        return chunks if chunks else [text[:chunk_size]]


# ===============================================================
#  FAISS 后端（默认，开发/测试）
# ===============================================================


class LongTermMemory(EmbeddingMixin, HybridSearchMixin):
    """基于 FAISS 的长期记忆（IndexFlatIP + 归一化向量 == 余弦相似度）。

    构造::

        LongTermMemory(
            index_path="./vector_store/faiss_index",
            embedding_dim=1536,
            embedding_fn=None,   # 仅测试注入确定性向量，生产禁用
            settings=None,       # 默认取 config.Settings()
        )

    公共接口：add_document / add_documents_batch / search / asearch / save /
    load / load_knowledge_base / set_embedding_client。
    """

    def __init__(
        self,
        index_path: str = "./vector_store/faiss_index",
        embedding_dim: int = 1536,
        embedding_fn: Callable[[str], list[float]] | None = None,
        settings: Any = None,
    ):
        if faiss is None:
            logger.warning("faiss 不可用：向量检索为空，仅 BM25 可用（Embedding 仍显式校验）")
        self.index_path = Path(index_path)
        self.embedding_dim = int(embedding_dim)
        self._init_embedding(embedding_fn=embedding_fn, settings=settings)
        self._reset_state()
        self.load()

    # ---- 内部状态 ----

    @property
    def meta_path(self) -> Path:
        """文档元数据文件（与 faiss 索引同名 + .meta.json）。"""
        return self.index_path.with_suffix(".meta.json")

    def _reset_state(self) -> None:
        """清空全部状态：文档、去重表、BM25 缓存，并建立空索引。"""
        self._documents = []
        self._doc_by_id = {}
        self._content_to_id = {}
        self._bm25 = BM25Index()
        self._index = faiss.IndexFlatIP(self.embedding_dim) if faiss is not None else None

    def _register(self, doc: dict) -> None:
        """把一条已在 _documents 末尾的文档登记到去重表与 BM25 预分词缓存。"""
        self._doc_by_id[doc["id"]] = doc
        self._content_to_id[doc["content"]] = doc["id"]
        self._bm25.add(doc["id"], document_index_text(doc))

    def _init_index(self) -> bool:
        """兼容旧内部方法名：等价于 load()。"""
        return self.load()

    # ---- 持久化（G8）----

    def load(self) -> bool:
        """加载磁盘索引与元数据，返回是否成功加载已有索引。

        任一校验失败（维度不符 / meta 缺失 / ntotal 与文档数不一致 / JSON 非法）
        都会整体重置为空库，保证索引向量与文档元数据永远一一对应（G8）。
        """
        self._reset_state()
        if faiss is None or not self.index_path.exists():
            return False
        try:
            index = faiss.read_index(str(self.index_path))
            if int(index.d) != self.embedding_dim:
                raise ValueError(
                    "索引维度 " + str(int(index.d)) + " 与 settings.embedding_dim "
                    + str(self.embedding_dim) + " 不一致"
                )
            documents = self._read_meta_documents()
            if int(index.ntotal) != len(documents):
                raise ValueError(
                    "索引向量数 " + str(int(index.ntotal)) + " 与元数据文档数 "
                    + str(len(documents)) + " 不一致（索引错位，拒绝加载）"
                )
            for position, doc in enumerate(documents):
                if not isinstance(doc, dict) or "id" not in doc or "content" not in doc:
                    raise ValueError("元数据第 " + str(position) + " 条缺少 id/content 字段")
            self._index = index
            for doc in documents:
                normalized = {
                    "id": str(doc["id"]),
                    "content": str(doc["content"]),
                    "source": str(doc.get("source", "") or ""),
                    "metadata": dict(doc.get("metadata") or {}),
                }
                self._documents.append(normalized)
                self._register(normalized)
            logger.info(
                "已加载长期记忆索引：%d 篇文档 (%s)", len(self._documents), self.index_path
            )
            return True
        except Exception as exc:
            logger.warning(
                "加载长期记忆失败（%s: %s）—— 已整体重置为空库，索引与元数据不会错位（G8）",
                type(exc).__name__,
                exc,
            )
            self._reset_state()
            return False

    def _read_meta_documents(self) -> list:
        meta_file = self.meta_path
        if not meta_file.exists():
            raise FileNotFoundError("元数据文件不存在: " + str(meta_file))
        with open(meta_file, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):  # 兼容旧格式（裸文档数组）
            return data
        if not isinstance(data, dict):
            raise ValueError("元数据格式非法（既不是 list 也不是 dict）")
        stored_dim = data.get("embedding_dim")
        if stored_dim is not None and int(stored_dim) != self.embedding_dim:
            raise ValueError(
                "元数据 embedding_dim=" + str(int(stored_dim))
                + " 与配置 " + str(self.embedding_dim) + " 不一致"
            )
        documents = data.get("documents")
        if not isinstance(documents, list):
            raise ValueError("元数据缺少 documents 列表")
        return documents

    def save(self) -> None:
        """同时持久化 faiss 索引与 meta.json（临时文件 + os.replace 原子替换）。"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if self._index is not None:
            self._atomic_write(self.index_path, self._write_index)
        payload = {
            "version": 2,
            "embedding_dim": self.embedding_dim,
            "documents": self._documents,
        }
        self._atomic_write(self.meta_path, lambda tmp: self._write_meta(tmp, payload))
        ntotal = int(self._index.ntotal) if self._index is not None else 0
        logger.info("长期记忆已保存：%d 篇文档 / %d 条向量 -> %s",
                    len(self._documents), ntotal, self.index_path)

    def _atomic_write(self, target: Path, writer: Callable[[Path], None]) -> None:
        tmp = target.with_name(target.name + ".tmp")
        try:
            writer(tmp)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover - Windows 偶发占用
                    logger.warning("临时文件清理失败: %s", tmp)

    def _write_index(self, tmp: Path) -> None:
        faiss.write_index(self._index, str(tmp))

    @staticmethod
    def _write_meta(tmp: Path, payload: dict) -> None:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    # ---- 写入 ----

    def add_document(
        self,
        content: str,
        source: str = "",
        metadata: dict | None = None,
        context: str | None = None,
    ) -> str:
        """新增文档，返回 doc_id = md5(content)。

        context：contextual retrieval 前缀（见 memory/ingest.py）——
        只参与向量化与 BM25 索引，不进入返回的 content，也不改变去重键。

        同内容重复调用直接返回旧 id（不重复入索引、不重复调用 Embedding）。
        Embedding 失败时抛 EmbeddingUnavailableError，且内部状态保持不变。
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content 必须为非空字符串")

        existing_id = self._content_to_id.get(content)
        if existing_id is not None:
            logger.debug("内容已存在，去重返回旧 id: %s", existing_id)
            return existing_id

        doc_id = hashlib.md5(content.encode("utf-8")).hexdigest()
        doc_meta = dict(metadata or {})
        if context and str(context).strip():
            doc_meta["context"] = str(context).strip()
        index_text = document_index_text({"content": content, "metadata": doc_meta})
        vector = self._embed_sync(index_text)  # 先算向量：失败则状态保持干净

        if self._index is not None:
            self._index.add(np.ascontiguousarray(vector.reshape(1, -1), dtype=np.float32))

        doc = {
            "id": doc_id,
            "content": content,
            "source": source,
            "metadata": doc_meta,
        }
        self._documents.append(doc)
        self._register(doc)
        return doc_id

    def add_documents_batch(self, documents: list) -> list:
        """批量新增（逐条走同一去重与 Embedding 校验）。"""
        doc_ids: list[str] = []
        for doc in documents:
            doc_ids.append(
                self.add_document(
                    content=doc.get("content", ""),
                    source=doc.get("source", ""),
                    metadata=doc.get("metadata") or {},
                    context=doc.get("context"),
                )
            )
        return doc_ids

    # ---- 向量检索 ----

    def _vector_search(self, query: str, top_k: int = 5) -> list[dict]:
        if self._index is None or self._index.ntotal == 0:
            return []
        return self._vector_search_with_vector(self._embed_sync(query), top_k)

    def _vector_search_with_vector(self, query_vector: Any, top_k: int = 5) -> list[dict]:
        """用已算好的向量检索（异步路径复用，避免在事件循环里重复取向量）。"""
        if self._index is None or self._index.ntotal == 0:
            return []
        k = int(min(max(int(top_k), 1), self._index.ntotal))
        scores, indices = self._index.search(
            np.ascontiguousarray(np.asarray(query_vector, dtype=np.float32).reshape(1, -1)), k
        )
        results: list[dict] = []
        for score, raw_index in zip(scores[0], indices[0], strict=True):
            position = int(raw_index)
            if position < 0 or position >= len(self._documents):
                continue
            doc = dict(self._documents[position])
            # IndexFlatIP + 归一化向量 => 内积即 cosine，裁剪到 [0,1] 作为相似度
            doc["score"] = to_unit_score(score)
            results.append(doc)
        return results

    # ---- 异步检索（生产主路径，T-P0-10）----

    async def asearch(self, query: str, top_k: int = 5) -> list[dict]:
        """异步 Hybrid Search。

        - Embedding：AsyncOpenAI（或注入的 async embedding_fn），不阻塞事件循环；
        - FAISS / BM25 / RRF：asyncio.to_thread()，避免 CPU 阻塞 loop。
        """
        if not self._documents:
            return []
        query_vector = await self.aembed(query)

        def _local_search() -> list[dict]:
            vector_results = self._vector_search_with_vector(query_vector, top_k=top_k * 2)
            bm25_results = self._bm25_search(query, top_k=top_k * 2)
            return self._merge_results(vector_results, bm25_results, query, top_k)

        return await asyncio.to_thread(_local_search)


# ===============================================================
#  Milvus 后端（生产可选，settings.vector_store_type = milvus）
# ===============================================================

try:  # pymilvus 未安装时静默降级为 _HAS_MILVUS=False（保持原有行为）
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )

    _HAS_MILVUS = True
except ImportError:  # pragma: no cover
    _HAS_MILVUS = False


class MilvusLongTermMemory(EmbeddingMixin, HybridSearchMixin):
    """Milvus 后端，与 LongTermMemory 暴露完全相同的公共接口。

    tokenize / BM25Index / rrf_fuse / EmbeddingMixin / HybridSearchMixin 全部复用
    共享实现（无第二份 BM25 代码），只有向量检索这一步调用 pymilvus。
    连接参数取自 settings.milvus_host / milvus_port / milvus_collection。
    """

    def __init__(
        self,
        embedding_dim: int = 1536,
        embedding_fn: Callable[[str], list[float]] | None = None,
        settings: Any = None,
        host: str | None = None,
        port: int | None = None,
        collection: str | None = None,
        **_kwargs: Any,
    ):
        if not _HAS_MILVUS:
            raise ImportError(
                "pymilvus 未安装。请执行: pip install pymilvus\n"
                "或切换为 FAISS（设置 settings.vector_store_type='faiss'）"
            )
        if settings is None:
            if Settings is None:
                raise RuntimeError("无法导入 config.Settings，请显式传入 settings")
            settings = Settings()
        self.embedding_dim = int(embedding_dim)
        self.milvus_host = str(host or getattr(settings, "milvus_host", "localhost"))
        self.milvus_port = int(port or getattr(settings, "milvus_port", 19530))
        self.collection_name = str(
            collection or getattr(settings, "milvus_collection", "heteroagent_docs")
        )
        self._init_embedding(embedding_fn=embedding_fn, settings=settings)
        self._documents = []
        self._doc_by_id = {}
        self._content_to_id = {}
        self._bm25 = BM25Index()
        self._collection: Any = None
        self._connect()
        self._init_collection()

    # ---- 连接与集合 ----

    def _connect(self) -> None:
        try:
            connections.connect(alias="default", host=self.milvus_host, port=str(self.milvus_port))
            logger.info("Milvus 已连接: %s:%s", self.milvus_host, self.milvus_port)
        except Exception as exc:
            logger.error("Milvus 连接失败: %s", exc)
            raise

    def _init_collection(self) -> None:
        if utility.has_collection(self.collection_name):
            self._collection = Collection(self.collection_name)
            self._collection.load()
            logger.info("Milvus 集合已加载: %s", self.collection_name)
            return
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.embedding_dim),
        ]
        schema = CollectionSchema(fields=fields, description="HeteroAgent 知识库文档")
        self._collection = Collection(name=self.collection_name, schema=schema)
        self._collection.create_index(
            field_name="embedding",
            index_params={"metric_type": "IP", "index_type": "IVF_FLAT", "params": {"nlist": 128}},
        )
        self._collection.load()
        logger.info("Milvus 集合已创建: %s", self.collection_name)

    def _register(self, doc: dict) -> None:
        self._doc_by_id[doc["id"]] = doc
        self._content_to_id[doc["content"]] = doc["id"]
        self._bm25.add(doc["id"], document_index_text(doc))

    # ---- 写入 ----

    def add_document(
        self,
        content: str,
        source: str = "",
        metadata: dict | None = None,
        context: str | None = None,
    ) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content 必须为非空字符串")
        existing_id = self._content_to_id.get(content)
        if existing_id is not None:
            return existing_id
        doc_id = hashlib.md5(content.encode("utf-8")).hexdigest()
        doc_meta = dict(metadata or {})
        if context and str(context).strip():
            doc_meta["context"] = str(context).strip()
        index_text = document_index_text({"content": content, "metadata": doc_meta})
        vector = self._embed_sync(index_text)  # Embedding 失败 -> 抛错且不写库
        self._collection.insert(
            [[doc_id], [content[:65535]], [str(source)[:512]], [vector.tolist()]]
        )
        doc = {
            "id": doc_id,
            "content": content,
            "source": source,
            "metadata": doc_meta,
        }
        self._documents.append(doc)
        self._register(doc)
        return doc_id

    def add_documents_batch(self, documents: list) -> list:
        doc_ids: list[str] = []
        for doc in documents:
            doc_ids.append(
                self.add_document(
                    content=doc.get("content", ""),
                    source=doc.get("source", ""),
                    metadata=doc.get("metadata") or {},
                    context=doc.get("context"),
                )
            )
        return doc_ids

    # ---- 检索 ----

    def _hits_to_results(self, hits: Any) -> list:
        results: list[dict] = []
        for hit in hits:
            doc_id = str(hit.id)
            doc = self._doc_by_id.get(doc_id)
            if doc is None:
                entity = getattr(hit, "entity", None)
                doc = {
                    "id": doc_id,
                    "content": str(entity.get("content", "")) if entity is not None else "",
                    "source": str(entity.get("source", "")) if entity is not None else "",
                    "metadata": {},
                }
            item = dict(doc)
            item["score"] = to_unit_score(hit.score)
            results.append(item)
        return results

    def _search_with_vector(self, query_vector: Any, top_k: int) -> list:
        if self._collection is None or not self._documents:
            return []
        try:
            raw = self._collection.search(
                data=[np.asarray(query_vector, dtype=np.float32).reshape(-1).tolist()],
                anns_field="embedding",
                param={"metric_type": "IP", "params": {"nprobe": 16}},
                limit=int(min(max(int(top_k), 1), len(self._documents))),
                output_fields=["content", "source"],
            )
        except Exception as exc:
            logger.warning("Milvus 向量检索失败: %s", exc)
            return []
        return self._hits_to_results(raw[0])

    def _vector_search(self, query: str, top_k: int = 5) -> list[dict]:
        return self._search_with_vector(self._embed_sync(query), top_k)

    def _vector_search_with_vector(self, query_vector: Any, top_k: int = 5) -> list[dict]:
        return self._search_with_vector(query_vector, top_k)

    # ---- 持久化 ----

    def save(self) -> None:
        """Milvus 自带持久化，这里执行 flush（公共接口与 FAISS 后端一致）。"""
        if self._collection is not None:
            self._collection.flush()
            logger.info("Milvus 集合已 flush: %s", self.collection_name)

    def load(self) -> bool:
        """从集合回灌内存文档缓存（公共接口与 FAISS 后端一致）。"""
        if self._collection is None:
            return False
        try:
            rows = self._collection.query(
                expr='id != ""', output_fields=["id", "content", "source"], limit=16384
            )
        except Exception as exc:
            logger.warning("Milvus 文档回灌失败（保留当前内存状态）: %s", exc)
            return False
        self._documents = []
        self._doc_by_id = {}
        self._content_to_id = {}
        self._bm25 = BM25Index()
        for row in rows or []:
            content = str(row.get("content", ""))
            if not content.strip():
                continue
            doc = {
                "id": str(row.get("id")),
                "content": content,
                "source": str(row.get("source", "") or ""),
                "metadata": {},
            }
            self._documents.append(doc)
            self._register(doc)
        return True

    async def asearch(self, query: str, top_k: int = 5) -> list:
        """异步检索（公共接口与 FAISS 后端一致，T-P0-10）。"""
        if not self._documents:
            return []
        query_vector = await self.aembed(query)

        def _local_search() -> list:
            vector_results = self._vector_search_with_vector(query_vector, top_k=top_k * 2)
            bm25_results = self._bm25_search(query, top_k=top_k * 2)
            return self._merge_results(vector_results, bm25_results, query, top_k)

        return await asyncio.to_thread(_local_search)


# ===============================================================
#  工厂：按 settings.vector_store_type 分发
# ===============================================================


def create_vector_store(settings: Any = None, **kwargs: Any):
    """按 settings.vector_store_type 选择后端（faiss / milvus），参数取自 settings。"""
    if settings is None:
        if Settings is None:
            raise RuntimeError("无法导入 config.Settings，请显式传入 settings")
        settings = Settings()

    store_type = str(getattr(settings, "vector_store_type", "faiss") or "faiss").strip().lower()
    kwargs.setdefault("settings", settings)
    kwargs.setdefault("embedding_dim", int(getattr(settings, "embedding_dim", 1536)))

    if store_type == "milvus":
        kwargs.setdefault("host", getattr(settings, "milvus_host", "localhost"))
        kwargs.setdefault("port", int(getattr(settings, "milvus_port", 19530)))
        kwargs.setdefault(
            "collection", getattr(settings, "milvus_collection", "heteroagent_docs")
        )
        logger.info(
            "向量库后端: Milvus (%s:%s / %s)",
            kwargs["host"], kwargs["port"], kwargs["collection"],
        )
        return MilvusLongTermMemory(**kwargs)

    kwargs.setdefault("index_path", getattr(settings, "faiss_index_path", "./vector_store/faiss_index"))
    logger.info("向量库后端: FAISS (%s)", kwargs["index_path"])
    return LongTermMemory(**kwargs)

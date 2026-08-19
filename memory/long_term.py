"""
长期记忆 — 基于向量数据库的持久化记忆
存储用户画像、历史工单、知识库文档等需要持久化的信息。
支持语义相似度检索和BM25关键词检索（Hybrid Search），用于RAG知识检索Agent。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

import numpy as np

try:
    import faiss
except ImportError:
    faiss = None

logger = logging.getLogger(__name__)


class LongTermMemory:
    """
    长期记忆：基于FAISS的向量检索 + BM25关键词检索（Hybrid Search）。

    特点：
    - 向量化存储，支持语义相似度检索
    - 真实 Embedding 模型（OpenAI text-embedding-3-small）+ 降级回退
    - BM25关键词检索，擅长精确匹配（专有名词、订单号）
    - RRF融合排序，综合两种检索结果
    - 持久化到磁盘，跨会话保持
    - 支持增量更新和批量导入
    - 生产环境可切换为Milvus/Pinecone

    文档分块策略：
    - 固定长度分块 (512 tokens) + 重叠窗口 (128 tokens)
    - 按段落自然分割优先
    """

    def __init__(
        self,
        index_path: str = "./vector_store/faiss_index",
        embedding_dim: int = 1536,
    ):
        self.index_path = Path(index_path)
        self.embedding_dim = embedding_dim
        self._documents: list[dict[str, Any]] = []
        self._index = None
        self._embedding_cache: dict[str, np.ndarray] = {}  # 文本→向量缓存
        self._embedding_failed = False  # 标记 Embedding API 是否已失败，避免重复尝试
        self._init_embedding_model()
        self._init_index()

    def _init_embedding_model(self):
        """
        初始化 Embedding 模型（Q14）。
        优先使用 OpenAI Embedding API，不可用时降级为 hash 向量。
        启动时做一次探测调用，失败则标记跳过，避免运行时卡顿。
        """
        self._embedding_client = None
        try:
            from langchain_openai import OpenAIEmbeddings
            client = OpenAIEmbeddings(
                model="text-embedding-3-small",
                openai_api_key=os.getenv("OPENAI_API_KEY"),
                openai_api_base=os.getenv("OPENAI_BASE_URL"),
                request_timeout=5,
            )
            # 探测调用：成功才启用，失败则标记跳过
            try:
                client.embed_query("test")
                self._embedding_client = client
                self._embedding_failed = False
                logger.info("✅ Embedding 模型已加载: text-embedding-3-small")
            except Exception as e:
                self._embedding_client = None
                self._embedding_failed = True
                logger.warning(f"⚠️ Embedding API 探测失败，降级为简易向量: {e}")
        except Exception as e:
            self._embedding_client = None
            self._embedding_failed = True
            logger.warning(f"⚠️ OpenAI Embedding 初始化失败，降级为简易向量: {e}")

    def _init_index(self):
        """初始化FAISS索引"""
        if faiss is None:
            self._index = None
            return

        metadata_path = self.index_path.with_suffix(".meta.json")
        if self.index_path.exists():
            try:
                self._index = faiss.read_index(str(self.index_path))
                if metadata_path.exists():
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        self._documents = json.load(f)
            except Exception:
                self._index = faiss.IndexFlatIP(self.embedding_dim)
        else:
            self._index = faiss.IndexFlatIP(self.embedding_dim)

    def _simple_embedding(self, text: str) -> np.ndarray:
        """
        统一 Embedding 入口（Q14）。

        优先使用 OpenAI Embedding API 生成语义向量；
        API 不可用时自动降级为 hash-based 伪向量。
        结果按文本内容缓存，避免重复调用。
        """
        # ── 缓存命中 ──
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        vec = None

        # ── 优先: OpenAI Embedding API（已失败则跳过，避免重复等待超时）──
        if self._embedding_client is not None and not self._embedding_failed:
            try:
                vec = self._embedding_client.embed_query(text)
                vec = np.array(vec, dtype=np.float32)
                if vec.shape[0] != self.embedding_dim:
                    logger.warning(f"Embedding 维度不匹配: 期望 {self.embedding_dim}, 实际 {vec.shape[0]}")
                    vec = None  # 降级
            except Exception as e:
                logger.warning(f"OpenAI Embedding 调用失败，降级为简易向量: {e}")
                self._embedding_failed = True  # 标记失败，后续跳过
                vec = None

        # ── 降级: hash-based 伪向量 ──
        if vec is None:
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            np.random.seed(int(text_hash[:8], 16) % (2**32))
            vec = np.random.randn(self.embedding_dim).astype(np.float32)

        # 归一化
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        # 写入缓存
        self._embedding_cache[cache_key] = vec
        return vec

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        """添加文档到向量库"""
        doc_id = hashlib.md5(content.encode()).hexdigest()[:12]

        doc = {
            "id": doc_id,
            "content": content,
            "source": source,
            "metadata": metadata or {},
        }
        self._documents.append(doc)

        if self._index is not None:
            embedding = self._simple_embedding(content)
            self._index.add(embedding.reshape(1, -1))

        return doc_id

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        """批量添加文档"""
        doc_ids = []
        for doc in documents:
            doc_id = self.add_document(
                content=doc.get("content", ""),
                source=doc.get("source", ""),
                metadata=doc.get("metadata", {}),
            )
            doc_ids.append(doc_id)
        return doc_ids

    # ─── BM25 检索 ───

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中英文混合分词：按空格/标点切分 + 逐字中文"""
        # 按非字母数字中文字符切分
        tokens = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower())
        return tokens

    def _bm25_search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        BM25关键词检索。
        k1=1.5, b=0.75 为经典参数。
        """
        if not self._documents:
            return []

        k1, b = 1.5, 0.75
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        N = len(self._documents)
        avgdl = sum(len(self._tokenize(d["content"])) for d in self._documents) / N

        # 计算每个term的IDF
        idf: dict[str, float] = {}
        for term in set(query_terms):
            n_t = sum(1 for d in self._documents if term in self._tokenize(d["content"]))
            idf[term] = math.log((N - n_t + 0.5) / (n_t + 0.5) + 1)

        # 计算每个文档的BM25分数
        scores: list[tuple[float, int]] = []
        for idx, doc in enumerate(self._documents):
            doc_tokens = self._tokenize(doc["content"])
            dl = len(doc_tokens)
            tf_map: dict[str, int] = {}
            for t in doc_tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            score = 0.0
            for term in query_terms:
                if term not in tf_map:
                    continue
                tf = tf_map[term]
                numerator = tf * (k1 + 1)
                denominator = tf + k1 * (1 - b + b * dl / avgdl)
                score += idf[term] * numerator / denominator

            if score > 0:
                scores.append((score, idx))

        scores.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, idx in scores[:top_k]:
            doc = self._documents[idx].copy()
            doc["score"] = score
            results.append(doc)
        return results

    # ─── 向量检索 ───

    def _vector_search(self, query: str, top_k: int = 5) -> list[dict]:
        """FAISS向量语义检索"""
        if self._index is None or not self._documents:
            return []

        query_vec = self._simple_embedding(query).reshape(1, -1)
        scores, indices = self._index.search(query_vec, min(top_k, len(self._documents)))

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._documents):
                continue
            doc = self._documents[idx].copy()
            doc["score"] = float(score)
            results.append(doc)
        return results

    # ─── RRF 融合 ───

    @staticmethod
    def _rrf_fuse(
        vector_results: list[dict],
        bm25_results: list[dict],
        k: int = 60,
        top_k: int = 5,
    ) -> list[dict]:
        """
        Reciprocal Rank Fusion (RRF) 合并两路检索结果。
        score(d) = Σ 1 / (k + rank_i(d))
        """
        rrf_scores: dict[str, float] = {}
        doc_map: dict[str, dict] = {}

        for rank, doc in enumerate(vector_results, start=1):
            doc_id = doc["id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + 1.0 / (k + rank)
            doc_map[doc_id] = doc

        for rank, doc in enumerate(bm25_results, start=1):
            doc_id = doc["id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + 1.0 / (k + rank)
            doc_map[doc_id] = doc

        sorted_ids = sorted(rrf_scores, key=lambda d: rrf_scores[d], reverse=True)
        results = []
        for doc_id in sorted_ids[:top_k]:
            doc = doc_map[doc_id].copy()
            doc["rrf_score"] = rrf_scores[doc_id]
            results.append(doc)
        return results

    # ─── 统一检索入口 ───

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Hybrid Search：向量检索 + BM25关键词检索 → RRF融合排序。

        - 向量检索擅长理解语义（"怎么退钱" → "退款流程"）
        - BM25擅长精确匹配（专有名词、订单号）
        - RRF融合综合两者优势，通常比纯向量检索提升5-15%
        """
        if not self._documents:
            return []

        # 向量检索（FAISS不可用时降级为空）
        vector_results = self._vector_search(query, top_k=top_k * 2)

        # BM25关键词检索
        bm25_results = self._bm25_search(query, top_k=top_k * 2)

        # 如果两路都有结果 → RRF融合
        if vector_results and bm25_results:
            return self._rrf_fuse(vector_results, bm25_results, top_k=top_k)

        # 只有向量结果
        if vector_results:
            return vector_results[:top_k]

        # 只有BM25结果或纯关键词回退
        if bm25_results:
            return bm25_results[:top_k]

        # 最终回退：简单关键词匹配
        return self._fallback_search(query, top_k)

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """当FAISS不可用时的关键词回退搜索"""
        scored = []
        query_terms = set(query.lower().split())

        for doc in self._documents:
            content_lower = doc["content"].lower()
            score = sum(1 for term in query_terms if term in content_lower)
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_k]]

    def save(self):
        """持久化索引到磁盘"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self._index is not None:
            faiss.write_index(self._index, str(self.index_path))

        metadata_path = self.index_path.with_suffix(".meta.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(self._documents, f, ensure_ascii=False, indent=2)

    def load_knowledge_base(self, kb_dir: str) -> int:
        """从目录批量加载知识库文档"""
        kb_path = Path(kb_dir)
        if not kb_path.exists():
            return 0

        count = 0
        for file_path in kb_path.glob("**/*.txt"):
            content = file_path.read_text(encoding="utf-8")
            chunks = self._chunk_text(content)
            for chunk in chunks:
                self.add_document(
                    content=chunk,
                    source=str(file_path.name),
                    metadata={"file": str(file_path)},
                )
                count += 1

        return count

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        """
        文本分块：固定长度 + 重叠窗口。
        优先按段落分割，段落过长则按句子分割。
        """
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) <= chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
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


# ═══════════════════════════════════════════════════════════════
#  Milvus 向量数据库后端（Q11 — 生产环境替代 FAISS）
# ═══════════════════════════════════════════════════════════════

try:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )
    _HAS_MILVUS = True
except ImportError:
    _HAS_MILVUS = False


class MilvusLongTermMemory:
    """
    基于 Milvus 的长期记忆（Q11）。

    与 LongTermMemory (FAISS) 暴露相同的公共接口：
    - add_document / add_documents_batch
    - search (Hybrid: Milvus 向量检索 + BM25 + RRF)
    - save / load_knowledge_base

    需要运行中的 Milvus 服务（默认 localhost:19530）。
    环境变量：
      MILVUS_HOST  (默认 localhost)
      MILVUS_PORT  (默认 19530)
      MILVUS_COLLECTION (默认 heteroagent_docs)
    """

    COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "heteroagent_docs")
    MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
    MILVUS_PORT = int(os.getenv("MILVUS_PORT", "19530"))

    def __init__(self, embedding_dim: int = 1536, **_kwargs):
        if not _HAS_MILVUS:
            raise ImportError(
                "pymilvus 未安装。请执行: pip install pymilvus\n"
                "或切换为 FAISS（设置 VECTOR_STORE_TYPE=faiss）"
            )
        self.embedding_dim = embedding_dim
        self._documents: list[dict[str, Any]] = []
        self._collection: Collection | None = None
        self._embedding_client = None
        self._embedding_cache: dict[str, np.ndarray] = {}

        self._init_embedding_model()
        self._connect()
        self._init_collection()

    # ── Embedding（与 FAISS 版共享逻辑）──

    def _init_embedding_model(self):
        try:
            from langchain_openai import OpenAIEmbeddings
            self._embedding_client = OpenAIEmbeddings(
                model="text-embedding-3-small",
                openai_api_key=os.getenv("OPENAI_API_KEY"),
                openai_api_base=os.getenv("OPENAI_BASE_URL"),
            )
        except Exception:
            self._embedding_client = None

    def _get_embedding(self, text: str) -> list[float]:
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key].tolist()

        vec = None
        if self._embedding_client is not None:
            try:
                vec = self._embedding_client.embed_query(text)
            except Exception:
                vec = None

        if vec is None:
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            np.random.seed(int(text_hash[:8], 16) % (2**32))
            vec = np.random.randn(self.embedding_dim).tolist()

        arr = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        self._embedding_cache[cache_key] = arr
        return arr.tolist()

    # ── Milvus 连接与集合 ──

    def _connect(self):
        try:
            connections.connect(
                alias="default",
                host=self.MILVUS_HOST,
                port=self.MILVUS_PORT,
            )
            logger.info(f"✅ Milvus 已连接: {self.MILVUS_HOST}:{self.MILVUS_PORT}")
        except Exception as e:
            logger.error(f"❌ Milvus 连接失败: {e}")
            raise

    def _init_collection(self):
        if utility.has_collection(self.COLLECTION_NAME):
            self._collection = Collection(self.COLLECTION_NAME)
            self._collection.load()
            logger.info(f"✅ Milvus 集合已加载: {self.COLLECTION_NAME}")
            return

        # 创建集合 Schema
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.embedding_dim),
        ]
        schema = CollectionSchema(fields=fields, description="HeteroAgent 知识库文档")
        self._collection = Collection(name=self.COLLECTION_NAME, schema=schema)

        # 创建向量索引
        index_params = {
            "metric_type": "IP",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        self._collection.create_index(field_name="embedding", index_params=index_params)
        self._collection.load()
        logger.info(f"✅ Milvus 集合已创建: {self.COLLECTION_NAME}")

    # ── 公共接口（与 FAISS 版一致）──

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        doc_id = hashlib.md5(content.encode()).hexdigest()[:12]
        embedding = self._get_embedding(content)
        self._collection.insert([
            [doc_id],
            [content[:65535]],
            [source[:512]],
            [embedding],
        ])
        self._documents.append({"id": doc_id, "content": content, "source": source, "metadata": metadata or {}})
        return doc_id

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        doc_ids = []
        ids, contents, sources, embeddings = [], [], [], []
        for doc in documents:
            doc_id = hashlib.md5(doc.get("content", "").encode()).hexdigest()[:12]
            doc_ids.append(doc_id)
            ids.append(doc_id)
            contents.append(doc.get("content", "")[:65535])
            sources.append(doc.get("source", "")[:512])
            embeddings.append(self._get_embedding(doc.get("content", "")))
            self._documents.append({"id": doc_id, "content": doc.get("content", ""), "source": doc.get("source", ""), "metadata": doc.get("metadata", {})})
        self._collection.insert([ids, contents, sources, embeddings])
        return doc_ids

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Milvus 向量检索 + BM25 + RRF 融合"""
        if not self._documents:
            return []

        # 向量检索
        query_vec = self._get_embedding(query)
        search_params = {"metric_type": "IP", "params": {"nprobe": 16}}
        try:
            results = self._collection.search(
                data=[query_vec],
                anns_field="embedding",
                param=search_params,
                limit=top_k * 2,
                output_fields=["content", "source"],
            )
            vector_results = []
            for hit in results[0]:
                doc = {
                    "id": hit.id,
                    "content": hit.entity.get("content", ""),
                    "source": hit.entity.get("source", ""),
                    "score": float(hit.score),
                }
                vector_results.append(doc)
        except Exception as e:
            logger.warning(f"Milvus 向量检索失败: {e}")
            vector_results = []

        # BM25 关键词检索（复用 FAISS 版的逻辑）
        bm25_results = self._bm25_search(query, top_k * 2)

        # RRF 融合
        if vector_results and bm25_results:
            return self._rrf_fuse(vector_results, bm25_results, top_k=top_k)
        return (vector_results or bm25_results)[:top_k]

    # ── BM25 和 RRF（复用 FAISS 版逻辑）──

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower())

    def _bm25_search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self._documents:
            return []
        k1, b = 1.5, 0.75
        query_terms = self._tokenize(query)
        if not query_terms:
            return []
        N = len(self._documents)
        avgdl = sum(len(self._tokenize(d["content"])) for d in self._documents) / max(N, 1)
        idf: dict[str, float] = {}
        for term in set(query_terms):
            n_t = sum(1 for d in self._documents if term in self._tokenize(d["content"]))
            idf[term] = math.log((N - n_t + 0.5) / (n_t + 0.5) + 1)
        scores: list[tuple[float, int]] = []
        for idx, doc in enumerate(self._documents):
            doc_tokens = self._tokenize(doc["content"])
            dl = len(doc_tokens)
            tf_map: dict[str, int] = {}
            for t in doc_tokens:
                tf_map[t] = tf_map.get(t, 0) + 1
            score = sum(
                idf[term] * (tf_map[term] * (k1 + 1)) / (tf_map[term] + k1 * (1 - b + b * dl / max(avgdl, 1)))
                for term in query_terms if term in tf_map
            )
            if score > 0:
                scores.append((score, idx))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [{"id": self._documents[idx]["id"], "content": self._documents[idx]["content"], "source": self._documents[idx].get("source", ""), "score": s} for s, idx in scores[:top_k]]

    @staticmethod
    def _rrf_fuse(vector_results: list[dict], bm25_results: list[dict], k: int = 60, top_k: int = 5) -> list[dict]:
        rrf_scores: dict[str, float] = {}
        doc_map: dict[str, dict] = {}
        for rank, doc in enumerate(vector_results, start=1):
            did = doc["id"]
            rrf_scores[did] = rrf_scores.get(did, 0) + 1.0 / (k + rank)
            doc_map[did] = doc
        for rank, doc in enumerate(bm25_results, start=1):
            did = doc["id"]
            rrf_scores[did] = rrf_scores.get(did, 0) + 1.0 / (k + rank)
            doc_map[did] = doc
        sorted_ids = sorted(rrf_scores, key=lambda d: rrf_scores[d], reverse=True)
        results = []
        for did in sorted_ids[:top_k]:
            doc = doc_map[did].copy()
            doc["rrf_score"] = rrf_scores[did]
            results.append(doc)
        return results

    def save(self):
        """Milvus 自动持久化，此方法仅为接口兼容"""
        self._collection.flush()
        logger.info("Milvus 集合已 flush 到磁盘")

    def load_knowledge_base(self, kb_dir: str) -> int:
        """从目录批量加载知识库文档"""
        kb_path = Path(kb_dir)
        if not kb_path.exists():
            return 0
        count = 0
        for file_path in kb_path.glob("**/*.txt"):
            content = file_path.read_text(encoding="utf-8")
            chunks = LongTermMemory._chunk_text(content)
            for chunk in chunks:
                self.add_document(content=chunk, source=str(file_path.name), metadata={"file": str(file_path)})
                count += 1
        return count


# ═══════════════════════════════════════════════════════════════
#  工厂函数 — 根据 VECTOR_STORE_TYPE 自动选择后端
# ═══════════════════════════════════════════════════════════════

def create_vector_store(**kwargs) -> LongTermMemory | MilvusLongTermMemory:
    """
    向量数据库工厂（Q11）。

    环境变量 VECTOR_STORE_TYPE 控制：
      faiss  → LongTermMemory（默认，开发/测试）
      milvus → MilvusLongTermMemory（生产环境，需要 Milvus 服务）
    """
    store_type = os.getenv("VECTOR_STORE_TYPE", "faiss").lower()

    if store_type == "milvus":
        logger.info("📦 使用 Milvus 作为向量数据库")
        return MilvusLongTermMemory(**kwargs)
    else:
        logger.info("📦 使用 FAISS 作为向量数据库")
        return LongTermMemory(**kwargs)

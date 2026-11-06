"""memory 层离线测试：G7 / G8 / G13 / T-P0-10 + 检索分数契约。

覆盖：
- G7：彻底没有 hash 伪向量；Embedding 不可用（allow_embedding_fallback 为 False/True）
      一律抛 EmbeddingUnavailableError；有界 LRU 向量缓存。
- G8：md5(content) 去重；save/load 原子往返；索引与元数据错位时整体重置。
- G13：tiktoken 计数、失败降级 len//2 且只告警一次。
- T-P0-10：asearch 走 AsyncOpenAI / to_thread，结果与 search 一致。
- 分数契约：每条结果同时含 score ∈ [0,1] 与 rrf_score（原始 RRF，仅供排序）。
- BM25：入库预分词、IDF 脏标记增量重建、查询不全库重分词、耗时随文档数线性。
- short_term：fakeredis 打桩验证 add/get/ttl/ltrim，tiktoken 截断与内存分支 LRU。

全部离线：不访问网络，不依赖 OPENAI_API_KEY。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import memory.long_term as lt_module
import memory.short_term as st_module
from config import Settings
from memory.long_term import (
    BM25Index,
    EmbeddingUnavailableError,
    HybridSearchMixin,
    LongTermMemory,
    MilvusLongTermMemory,
    create_vector_store,
    rrf_fuse,
    to_unit_score,
    tokenize,
)
from memory.short_term import ShortTermMemory, count_tokens

DIM = 64

# ── 概念槽位：同义概念共享槽位，"退款" 与 "退钱" 的相似度高于 "开户" ──
SEMANTIC_SLOTS = {
    "退款": 0, "退钱": 0, "退货": 0,
    "开户": 1, "销户": 1,
    "发票": 2, "开票": 2,
    "物流": 3, "快递": 3,
    "账户": 4, "密码": 5, "订单": 6, "人工": 7, "客服": 7,
}


def make_semantic_embedder(dim: int = DIM):
    """确定性"语义"向量：概念槽 + md5 长尾槽，用于替代 hash 伪向量做离线测试。"""

    def embed(text: str) -> list[float]:
        vec = [0.0] * dim
        units = re.findall(r"[一-龥]|[a-zA-Z0-9]+", str(text).lower())
        grams = units + [units[i] + units[i + 1] for i in range(len(units) - 1)]
        for gram in grams:
            slot = SEMANTIC_SLOTS.get(gram)
            if slot is not None and slot < dim:
                vec[slot] += 1.0
            else:
                digest = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
                vec[8 + digest % (dim - 8)] += 0.15
        arr = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        if norm <= 0.0:
            arr = np.zeros(dim, dtype=np.float32)
            arr[0] = 1.0
            norm = 1.0
        return (arr / norm).tolist()

    return embed


@pytest.fixture
def embedder():
    return make_semantic_embedder()


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        embedding_model="test-embedding",
        embedding_dim=DIM,
        embedding_timeout_seconds=0.5,
        allow_embedding_fallback=False,
        openai_api_key="",
        openai_base_url="http://127.0.0.1:9/v1",
        vector_store_type="faiss",
        faiss_index_path=str(tmp_path / "faiss_index"),
    )
    base.update(overrides)
    return Settings(**base)


def make_ltm(tmp_path, embedder_fn=None, index_name="faiss_index", **overrides) -> LongTermMemory:
    settings = make_settings(tmp_path, **overrides)
    return LongTermMemory(
        index_path=str(tmp_path / index_name),
        embedding_dim=DIM,
        embedding_fn=embedder_fn,
        settings=settings,
    )


CORPUS = [
    ("用户申请退款后，款项将在1到3个工作日内原路退回到支付账户。", "kb/refund.txt"),
    ("如需退钱，请先在订单详情页发起退款申请，审核通过后自动到账。", "kb/refund.txt"),
    ("开户流程：携带身份证前往营业厅办理，或在线上传身份证完成开户。", "kb/account.txt"),
    ("新用户开户赠送10元话费，销户需先结清已出账费用。", "kb/account.txt"),
    ("发票开具：订单完成后30天内可在线申请电子发票。", "kb/invoice.txt"),
    ("物流查询：点击订单即可查看快递单号与配送进度。", "kb/logistics.txt"),
]


# ═══════════════════════════════════════════════════════════
#  语义检索 / asearch / RRF
# ═══════════════════════════════════════════════════════════


class TestSemanticSearch:
    def test_embedder_semantics_selfcheck(self, embedder):
        query = np.asarray(embedder("怎么退钱"), dtype=np.float32)
        refund = np.asarray(embedder("退款流程说明"), dtype=np.float32)
        account = np.asarray(embedder("开户办理流程"), dtype=np.float32)
        assert float(np.dot(query, refund)) > float(np.dot(query, account))

    def test_search_ranking_refund_beats_account(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        for content, source in CORPUS:
            ltm.add_document(content, source=source)

        top2 = ltm.search("怎么退钱", top_k=2)
        assert len(top2) == 2
        assert all(("退款" in r["content"] or "退钱" in r["content"]) for r in top2)

        full = ltm.search("怎么退钱", top_k=6)
        first_refund = next(
            i for i, r in enumerate(full) if "退款" in r["content"] or "退钱" in r["content"]
        )
        first_account = next(i for i, r in enumerate(full) if "开户" in r["content"])
        assert first_refund < first_account

    def test_asearch_matches_search(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        for content, source in CORPUS:
            ltm.add_document(content, source=source)

        sync_results = ltm.search("怎么退钱", top_k=3)
        async_results = asyncio.run(ltm.asearch("怎么退钱", top_k=3))
        assert [r["id"] for r in async_results] == [r["id"] for r in sync_results]
        for item in async_results:
            assert {"id", "content", "source", "score", "rrf_score"} <= set(item)
            assert 0.0 <= item["score"] <= 1.0

    def test_rrf_fusion_scores_are_consistent(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        for content, source in CORPUS:
            ltm.add_document(content, source=source)

        query = "怎么退钱"
        vector_results = ltm._vector_search(query, top_k=12)
        bm25_results = ltm._bm25_search(query, top_k=12)
        assert vector_results and bm25_results

        fused = ltm.search(query, top_k=6)
        assert len(fused) == len(CORPUS)
        rrf_values = [r["rrf_score"] for r in fused]
        assert rrf_values == sorted(rrf_values, reverse=True)

        vector_rank = {r["id"]: i + 1 for i, r in enumerate(vector_results)}
        bm25_rank = {r["id"]: i + 1 for i, r in enumerate(bm25_results)}
        for item in fused:
            expected = 0.0
            if item["id"] in vector_rank:
                expected += 1.0 / (60 + vector_rank[item["id"]])
            if item["id"] in bm25_rank:
                expected += 1.0 / (60 + bm25_rank[item["id"]])
            assert item["rrf_score"] == pytest.approx(expected, abs=1e-9)

        both = set(vector_rank) & set(bm25_rank)
        single = (set(vector_rank) | set(bm25_rank)) - both
        scores = {r["id"]: r["rrf_score"] for r in fused}
        for hit_both in both:
            for hit_single in single:
                assert scores[hit_both] > scores[hit_single]

    def test_rrf_fuse_is_module_level_shared(self):
        vector = [{"id": "a", "content": "a", "source": "", "score": 0.9}]
        bm25 = [{"id": "b", "content": "b", "source": "", "score": 1.0}]
        fused = rrf_fuse(vector, bm25, k=60, top_k=2)
        assert [r["id"] for r in fused] == ["a", "b"]
        assert fused[0]["rrf_score"] == pytest.approx(1.0 / 61)
        assert fused[1]["rrf_score"] == pytest.approx(1.0 / 61)


# ═══════════════════════════════════════════════════════════
#  分数契约（knowledge_rag 置信度阈值依赖）
# ═══════════════════════════════════════════════════════════


class TestScoreContract:
    def test_every_result_has_unit_score_and_rrf(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        for content, source in CORPUS:
            ltm.add_document(content, source=source)

        results = ltm.search("怎么退钱", top_k=6)
        assert len(results) == 6
        for item in results:
            assert {"id", "content", "source", "score", "rrf_score"} <= set(item)
            assert 0.0 <= item["score"] <= 1.0
            assert item["rrf_score"] > 0.0
        rrf_values = [r["rrf_score"] for r in results]
        assert rrf_values == sorted(rrf_values, reverse=True)

    def test_vector_score_is_clipped_cosine(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        for content, source in CORPUS:
            ltm.add_document(content, source=source)

        query_vec = np.asarray(embedder("怎么退钱"), dtype=np.float32)
        for item in ltm.search("怎么退钱", top_k=6):
            doc_vec = np.asarray(embedder(item["content"]), dtype=np.float32)
            cosine = float(np.dot(query_vec, doc_vec))
            assert item["score"] == pytest.approx(max(0.0, cosine), abs=1e-5)

    def test_semantically_similar_doc_scores_higher(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        refund_id = ltm.add_document("退款将在1到3个工作日内原路退回。", source="kb")
        account_id = ltm.add_document("开户需要携带身份证到营业厅办理。", source="kb")
        results = {r["id"]: r for r in ltm.search("怎么退钱", top_k=5)}
        assert results[refund_id]["score"] > results[account_id]["score"]
        assert 0.0 <= results[account_id]["score"] <= 1.0

    def test_bm25_channel_scores_normalized(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        for content, source in CORPUS:
            ltm.add_document(content, source=source)
        hits = ltm._bm25_search("怎么退钱", top_k=10)
        assert hits
        assert all(0.0 <= h["score"] <= 1.0 for h in hits)
        assert all(h["bm25_score"] > 0.0 for h in hits)
        assert max(h["score"] for h in hits) == pytest.approx(1.0)
        assert min(h["score"] for h in hits) == pytest.approx(0.0)

    def test_to_unit_score_helper(self):
        assert to_unit_score(1.7) == 1.0
        assert to_unit_score(-0.3) == 0.0
        assert to_unit_score(float("nan")) == 0.0
        assert to_unit_score("not-a-number") == 0.0
        assert to_unit_score(0.42) == pytest.approx(0.42)


# ═══════════════════════════════════════════════════════════
#  去重 / save-load / 错位防护（G8）
# ═══════════════════════════════════════════════════════════


class TestDedupAndPersistence:
    def test_duplicate_content_returns_same_id(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        content = "退款将在1-3个工作日到账。"
        first_id = ltm.add_document(content, source="a")
        second_id = ltm.add_document(content, source="b")
        assert first_id == second_id == hashlib.md5(content.encode("utf-8")).hexdigest()
        assert len(ltm._documents) == 1
        assert ltm._index.ntotal == 1

        ids = ltm.add_documents_batch([{"content": content}, {"content": "开户需要身份证。"}])
        assert ids[0] == first_id
        assert len(ltm._documents) == 2
        assert ltm._index.ntotal == 2

        results = ltm.search("退款", top_k=5)
        assert len({r["id"] for r in results}) == len(results)

    def test_save_load_roundtrip(self, tmp_path, embedder):
        index_path = str(tmp_path / "faiss_index")
        ltm = LongTermMemory(
            index_path=index_path,
            embedding_dim=DIM,
            embedding_fn=embedder,
            settings=make_settings(tmp_path),
        )
        for content, source in CORPUS:
            ltm.add_document(content, source=source)
        expected = ltm.search("怎么退钱", top_k=3)

        ltm.save()
        assert Path(index_path).exists()
        assert Path(index_path + ".meta.json").exists()

        reloaded = LongTermMemory(
            index_path=index_path,
            embedding_dim=DIM,
            embedding_fn=embedder,
            settings=make_settings(tmp_path),
        )
        assert reloaded._index.ntotal == len(CORPUS)
        assert len(reloaded._documents) == len(CORPUS)
        results = reloaded.search("怎么退钱", top_k=3)
        assert [r["id"] for r in results] == [r["id"] for r in expected]
        assert [r["score"] for r in results] == pytest.approx([r["score"] for r in expected])

        # 增量写入后 load() 应回到磁盘上的已保存状态
        reloaded.add_document("会员积分兑换规则说明。", source="kb/points.txt")
        assert len(reloaded._documents) == len(CORPUS) + 1
        assert reloaded.load() is True
        assert reloaded._index.ntotal == len(CORPUS)
        assert len(reloaded._documents) == len(CORPUS)

    def test_load_meta_mismatch_resets_state(self, tmp_path, embedder):
        index_path = str(tmp_path / "faiss_index")
        ltm = make_ltm(tmp_path, embedder)
        ltm.add_document("退款说明A", source="x")
        ltm.add_document("开户说明B", source="x")
        ltm.save()

        meta_path = Path(index_path + ".meta.json")
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["documents"] = payload["documents"][:1]  # 故意制造 2 向量 / 1 文档错位
        meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        broken = LongTermMemory(
            index_path=index_path,
            embedding_dim=DIM,
            embedding_fn=embedder,
            settings=make_settings(tmp_path),
        )
        # G8：加载失败必须同时清空 _documents，索引重建为空，绝不出现错位
        assert broken._documents == []
        assert broken._index.ntotal == 0
        assert broken.search("退款") == []

        # 重置后仍可正常写入，索引与文档数保持 1:1
        broken.add_document("恢复后新增的退款文档。", source="x")
        assert len(broken._documents) == 1
        assert broken._index.ntotal == 1

    def test_missing_meta_resets_state(self, tmp_path, embedder):
        index_path = str(tmp_path / "faiss_index")
        ltm = make_ltm(tmp_path, embedder)
        ltm.add_document("退款说明", source="x")
        ltm.save()
        Path(index_path + ".meta.json").unlink()

        broken = LongTermMemory(
            index_path=index_path,
            embedding_dim=DIM,
            embedding_fn=embedder,
            settings=make_settings(tmp_path),
        )
        assert broken._documents == []
        assert broken._index.ntotal == 0

    def test_legacy_list_meta_supported(self, tmp_path, embedder):
        index_path = str(tmp_path / "faiss_index")
        ltm = make_ltm(tmp_path, embedder)
        ltm.add_document("退款说明", source="x")
        ltm.add_document("开户说明", source="x")
        ltm.save()

        meta_path = Path(index_path + ".meta.json")
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        meta_path.write_text(json.dumps(payload["documents"], ensure_ascii=False), encoding="utf-8")

        reloaded = LongTermMemory(
            index_path=index_path,
            embedding_dim=DIM,
            embedding_fn=embedder,
            settings=make_settings(tmp_path),
        )
        assert len(reloaded._documents) == 2
        assert reloaded._index.ntotal == 2

    def test_load_knowledge_base_chunks_and_dedups(self, tmp_path, embedder):
        kb = tmp_path / "kb"
        kb.mkdir()
        text = ("第一段退款说明。" * 40) + "\n\n" + ("第二段开户说明。" * 40)
        (kb / "refund.txt").write_text(text, encoding="utf-8")

        ltm = make_ltm(tmp_path, embedder)
        assert ltm.load_knowledge_base(str(kb)) == 2
        assert ltm._index.ntotal == 2
        assert ltm.load_knowledge_base(str(kb)) == 0  # 重复导入被去重
        assert ltm.load_knowledge_base(str(tmp_path / "missing")) == 0


# ═══════════════════════════════════════════════════════════
#  Embedding 不可用（G7）
# ═══════════════════════════════════════════════════════════


class TestEmbeddingUnavailable:
    def test_add_document_raises_without_embedding(self, tmp_path):
        ltm = make_ltm(tmp_path, None)
        with pytest.raises(EmbeddingUnavailableError):
            ltm.add_document("退款说明")
        assert ltm._documents == []
        assert ltm._index.ntotal == 0

    def test_search_raises_without_embedding_after_load(self, tmp_path, embedder):
        writer = make_ltm(tmp_path, embedder)
        for content, source in CORPUS[:2]:
            writer.add_document(content, source=source)
        writer.save()

        reader = make_ltm(tmp_path, None)  # 无 embedding_fn、无 API key
        assert len(reader._documents) == 2
        with pytest.raises(EmbeddingUnavailableError):
            reader.search("怎么退钱")
        with pytest.raises(EmbeddingUnavailableError):
            asyncio.run(reader.asearch("怎么退钱"))

    def test_allow_fallback_true_still_raises_and_warns(self, tmp_path, embedder, caplog):
        writer = make_ltm(tmp_path, embedder)
        writer.add_document(CORPUS[0][0], source="x")
        writer.save()

        reader = make_ltm(tmp_path, None, allow_embedding_fallback=True)
        with caplog.at_level(logging.WARNING, logger="memory.long_term"):
            with pytest.raises(EmbeddingUnavailableError):
                reader.search("怎么退钱")
        assert "伪向量" in caplog.text

    def test_dimension_mismatch_raises_and_keeps_state_clean(self, tmp_path):
        ltm = make_ltm(tmp_path, lambda text: [0.1] * (DIM + 8))
        with pytest.raises(EmbeddingUnavailableError):
            ltm.add_document("维度不符的文档")
        assert ltm._documents == []
        assert ltm._index.ntotal == 0

    def test_no_pseudo_vector_code_left(self):
        """AST 级检查：剥离注释/文档字符串/字符串常量后，真实代码中不得出现伪向量。"""
        import ast

        source = Path(lt_module.__file__).read_text(encoding="utf-8")

        class _StripStrings(ast.NodeTransformer):
            def visit_Constant(self, node):
                if isinstance(node.value, str):
                    return ast.copy_location(ast.Constant(value=""), node)
                return node

        code_only = ast.unparse(_StripStrings().visit(ast.parse(source)))
        for token in ("np.random.seed", "randn", "sha256", "RandomState", "default_rng"):
            assert token not in code_only, "long_term.py 中残留伪向量调用: " + token


class TestClientInjection:
    @staticmethod
    def _fake_client(embedder, async_mode=False):
        calls = {"n": 0}

        def create(model=None, input=None, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(data=[SimpleNamespace(embedding=embedder(input[0]))])

        if async_mode:

            async def async_create(model=None, input=None, **kwargs):
                return create(model=model, input=input)

            client = SimpleNamespace(embeddings=SimpleNamespace(create=async_create))
        else:
            client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        client.calls = calls
        return client

    def test_set_embedding_client_sync(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, None)
        client = self._fake_client(embedder)
        ltm.set_embedding_client(sync_client=client)
        ltm.add_document("退款流程说明", source="kb")
        ltm.add_document("开户流程说明", source="kb")
        results = ltm.search("怎么退钱", top_k=2)
        assert results and "退款" in results[0]["content"]
        assert client.calls["n"] >= 3  # 两次入库 + 一次查询

    def test_set_embedding_client_async_asearch(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, None)
        sync_client = self._fake_client(embedder)
        async_client = self._fake_client(embedder, async_mode=True)
        ltm.set_embedding_client(sync_client=sync_client, async_client=async_client)
        ltm.add_document("退款流程说明", source="kb")
        ltm.add_document("开户流程说明", source="kb")

        results = asyncio.run(ltm.asearch("怎么退钱", top_k=2))
        assert results and "退款" in results[0]["content"]
        assert async_client.calls["n"] >= 1  # asearch 确实走了异步客户端


class TestEmbeddingCache:
    def test_cache_is_bounded_ordereddict(self, tmp_path, embedder):
        ltm = make_ltm(tmp_path, embedder)
        assert isinstance(ltm._embedding_cache, OrderedDict)
        for i in range(4100):
            ltm._embed_sync("cache-probe-" + str(i))
        assert len(ltm._embedding_cache) == 4096
        oldest = hashlib.md5("cache-probe-0".encode()).hexdigest()
        newest = hashlib.md5("cache-probe-4099".encode()).hexdigest()
        assert oldest not in ltm._embedding_cache
        assert newest in ltm._embedding_cache

    def test_cache_hit_skips_recompute(self, tmp_path):
        calls = {"n": 0}

        def counting_embed(text):
            calls["n"] += 1
            vec = np.zeros(DIM, dtype=np.float32)
            vec[0] = 1.0
            return vec.tolist()

        ltm = make_ltm(tmp_path, counting_embed)
        ltm.add_document("同一份文档", source="x")
        assert calls["n"] == 1
        ltm.search("同一份文档", top_k=1)  # 查询文本与文档相同 -> 缓存命中
        assert calls["n"] == 1
        ltm.search("另一段查询", top_k=1)
        assert calls["n"] == 2


# ═══════════════════════════════════════════════════════════
#  BM25 增量 / 线性
# ═══════════════════════════════════════════════════════════


class TestBM25:
    def test_pre_tokenized_and_idf_incremental(self, tmp_path, embedder, monkeypatch):
        calls = {"n": 0}
        real_tokenize = lt_module.tokenize

        def counting_tokenize(text):
            calls["n"] += 1
            return real_tokenize(text)

        monkeypatch.setattr(lt_module, "tokenize", counting_tokenize)

        ltm = make_ltm(tmp_path, embedder)
        for i in range(20):
            ltm.add_document("订单" + str(i) + "的物流信息 w" + str(i % 5) + " common" + str(i), source="t")
        assert calls["n"] == 20  # 入库时一次性预分词
        assert ltm._bm25.dirty is True

        rare = "rareterm"
        assert ltm._bm25.idf_of(rare) is None  # 尚未出现在语料中

        ltm.add_document(rare + " 只在这份文档里出现", source="t")
        idf_before = ltm._bm25.idf_of(rare)
        assert idf_before is not None

        ltm.add_document("另一份文档也提到 " + rare, source="t")
        idf_after = ltm._bm25.idf_of(rare)
        assert idf_after is not None
        assert idf_after < idf_before  # 语料变更后 IDF 被重建（稀有度下降）

        # 查询阶段不做全库重分词：3 次查询只新增 3 次 tokenize 调用
        before_queries = calls["n"]
        for query in (rare, "common3", "物流 订单"):
            ltm.search(query, top_k=3)
        assert calls["n"] - before_queries == 3

    def test_query_time_scales_linearly(self, tmp_path, embedder):
        def build(count, index_name):
            ltm = make_ltm(tmp_path, embedder, index_name=index_name)
            for i in range(count):
                words = " ".join("w" + str((i * 7 + j) % 1009) for j in range(30))
                ltm.add_document("doc" + str(i) + " " + words, source="perf")
            return ltm

        small = build(300, "perf_small")
        big = build(1200, "perf_big")
        query = "w123 w456 w789"

        def bench(ltm, reps=30):
            ltm.search(query, top_k=5)  # 预热：触发 IDF 重建
            started = time.perf_counter()
            for _ in range(reps):
                ltm.search(query, top_k=5)
            return time.perf_counter() - started

        small_elapsed = bench(small)
        big_elapsed = bench(big)
        # 4 倍文档量 -> 查询耗时近似线性；放宽到 12 倍以吸收调度噪声
        assert big_elapsed < max(small_elapsed * 12.0, 1.0)
        assert big_elapsed < 5.0


# ═══════════════════════════════════════════════════════════
#  工厂与 Milvus 共享实现
# ═══════════════════════════════════════════════════════════


class TestFactoryAndSharedCode:
    def test_faiss_dispatch_from_settings(self, tmp_path, embedder):
        settings = make_settings(tmp_path, faiss_index_path=str(tmp_path / "factory_index"))
        store = create_vector_store(settings=settings, embedding_fn=embedder)
        assert isinstance(store, LongTermMemory)
        assert store.index_path == Path(str(tmp_path / "factory_index"))
        store.add_document("退款说明", source="kb")
        assert store.search("退款", top_k=1)[0]["content"].startswith("退款")

    def test_milvus_dispatch_passes_settings_params(self, tmp_path, embedder, monkeypatch):
        recorded = {}

        class StubMilvus:
            def __init__(self, **kwargs):
                recorded.update(kwargs)

        monkeypatch.setattr(lt_module, "MilvusLongTermMemory", StubMilvus)
        settings = make_settings(
            tmp_path,
            vector_store_type="milvus",
            milvus_host="10.0.0.9",
            milvus_port=29530,
            milvus_collection="kb_test",
        )
        store = create_vector_store(settings=settings, embedding_fn=embedder)
        assert isinstance(store, StubMilvus)
        assert recorded["host"] == "10.0.0.9"
        assert recorded["port"] == 29530
        assert recorded["collection"] == "kb_test"
        assert recorded["embedding_dim"] == DIM
        assert recorded["settings"] is settings
        assert recorded["embedding_fn"] is embedder

    def test_milvus_reuses_single_bm25_and_rrf_implementation(self):
        assert issubclass(MilvusLongTermMemory, HybridSearchMixin)
        assert issubclass(LongTermMemory, HybridSearchMixin)
        assert MilvusLongTermMemory._bm25_search is HybridSearchMixin._bm25_search
        assert MilvusLongTermMemory._rrf_fuse is HybridSearchMixin._rrf_fuse
        assert LongTermMemory._bm25_search is HybridSearchMixin._bm25_search
        assert isinstance(BM25Index(), BM25Index)

        source = Path(lt_module.__file__).read_text(encoding="utf-8")
        assert source.count("def _bm25_search") == 1
        assert source.count("def rrf_fuse") == 1
        assert source.count("def tokenize") == 1

    def test_milvus_unavailable_degrades_silently(self, monkeypatch):
        monkeypatch.setattr(lt_module, "_HAS_MILVUS", False)
        with pytest.raises(ImportError):
            MilvusLongTermMemory(settings=make_settings(Path(".")))


# ═══════════════════════════════════════════════════════════
#  short_term（fakeredis 打桩）
# ═══════════════════════════════════════════════════════════


class TestShortTermMemory:
    @staticmethod
    def _make(max_turns=3, ttl_seconds=60, **kwargs):
        import fakeredis.aioredis

        fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
        stm = ShortTermMemory(
            redis_client=fake, max_turns=max_turns, ttl_seconds=ttl_seconds, **kwargs
        )
        return stm, fake

    def test_redis_add_get_trim_ttl(self):
        stm, fake = self._make(max_turns=3, ttl_seconds=120)

        async def scenario():
            for i in range(5):
                role = "user" if i % 2 == 0 else "assistant"
                await stm.add_message("s1", role, "消息" + str(i))
            history = await stm.get_history("s1")
            ttl = await fake.ttl(stm._session_key("s1"))
            return history, ttl

        history, ttl = asyncio.run(scenario())
        assert [m["content"] for m in history] == ["消息2", "消息3", "消息4"]
        assert history[0]["role"] == "user"
        assert 0 < ttl <= 120

        async def last_one():
            return await stm.get_history("s1", last_n=1)

        assert [m["content"] for m in asyncio.run(last_one())] == ["消息4"]

    def test_redis_clear(self):
        stm, fake = self._make()

        async def scenario():
            await stm.add_message("s2", "user", "你好")
            before = await stm.get_history("s2")
            await stm.clear("s2")
            after = await stm.get_history("s2")
            exists = await fake.exists(stm._session_key("s2"))
            return before, after, exists

        before, after, exists = asyncio.run(scenario())
        assert len(before) == 1
        assert after == []
        assert exists == 0

    def test_context_window_truncation(self, monkeypatch):
        stm, _ = self._make(max_turns=10)
        monkeypatch.setattr(st_module, "count_tokens", lambda text: len(text))

        async def scenario():
            for _ in range(4):
                await stm.add_message("cw", "user", "u" * 100)
            return await stm.get_context_window("cw", max_tokens=250)

        window = asyncio.run(scenario())
        lines = window.split("\n")
        assert len(lines) == 2  # 106 * 2 = 212 <= 250 < 318
        assert lines[-1].endswith("u" * 100)

    def test_context_window_uses_token_budget(self):
        stm, _ = self._make(max_turns=10)

        async def scenario():
            await stm.add_message("g13", "user", "退款什么时候到账？")
            return await stm.get_context_window("g13", max_tokens=4000)

        window = asyncio.run(scenario())
        assert "退款什么时候到账" in window

    def test_fallback_lru_session_cap(self):
        stm = ShortTermMemory(
            redis_url="redis://127.0.0.1:1/0", max_turns=5, ttl_seconds=60, max_sessions=3
        )

        async def scenario():
            for session in ("s1", "s2", "s3", "s4"):
                await stm.add_message(session, "user", session + " 的消息")
            await stm.get_history("s2")  # 触达 s2 -> s3 成为最久未使用
            await stm.add_message("s5", "user", "s5 的消息")
            return list(stm._fallback_store.keys())

        keys = asyncio.run(scenario())
        assert len(keys) == 3
        assert "s3" not in keys
        assert "s5" in keys
        assert stm._redis_broken is True

    def test_count_tokens_accuracy(self):
        text = "Hello world, 退款 123"
        tokens = count_tokens(text)
        assert 0 < tokens <= len(text)
        assert count_tokens("") == 0

    def test_tiktoken_failure_warns_once_and_falls_back(self, monkeypatch, caplog):
        if st_module.tiktoken is None:
            pytest.skip("tiktoken 未安装")
        monkeypatch.setattr(st_module, "_TOKEN_ENCODER", None)
        monkeypatch.setattr(st_module, "_TOKEN_ENCODER_READY", False)

        def boom(name):
            raise RuntimeError("离线环境无法下载 cl100k_base")

        monkeypatch.setattr(st_module.tiktoken, "get_encoding", boom)
        with caplog.at_level(logging.WARNING, logger="memory.short_term"):
            first = st_module.count_tokens("退款说明")
            second = st_module.count_tokens("退款说明")
        assert first == len("退款说明") // 2  # 降级 len//2
        assert second == first
        warnings = [r for r in caplog.records if "降级" in r.getMessage()]
        assert len(warnings) == 1  # 只告警一次（G13）

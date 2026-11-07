"""知识库加载器测试（离线）。"""
from __future__ import annotations

from memory.kb_loader import corpus_stats, ingest_corpus, load_documents


def _write_kb(root):
    (root / "products").mkdir(parents=True)
    (root / "service").mkdir(parents=True)
    (root / "products" / "p01_测试产品.md").write_text(
        "# 测试产品甲\n\n> 合成演示数据，产品与条款均为虚构，仅用于检索系统测试。\n\n"
        "起投金额10000元，封闭期90天，风险等级R2。",
        encoding="utf-8",
    )
    (root / "service" / "s01_退款总则.md").write_text(
        "# 退款总则\n\n> 合成演示数据，政策与流程均为虚构，仅用于检索系统测试。\n\n"
        "购买后7天内可申请，3-5个工作日到账。",
        encoding="utf-8",
    )


def test_load_parses_title_category_and_strips_disclaimer(tmp_path):
    _write_kb(tmp_path)
    docs = load_documents(tmp_path)
    assert len(docs) == 2
    by_src = {d.source: d for d in docs}
    p = by_src["products/p01_测试产品.md"]
    assert p.title == "测试产品甲"
    assert p.category == "products"
    assert "合成演示数据" not in p.content   # 免责声明不进索引
    assert "10000元" in p.content


def test_missing_dir_returns_empty(tmp_path):
    assert load_documents(tmp_path / "nope") == []


def test_corpus_stats(tmp_path):
    _write_kb(tmp_path)
    stats = corpus_stats(load_documents(tmp_path))
    assert stats["documents"] == 2
    assert stats["categories"] == {"products": 1, "service": 1}
    assert stats["chars_min"] > 0
    assert stats["chars_avg"] > 0


def test_empty_stats():
    assert corpus_stats([]) == {"documents": 0}


class _Mem:
    def __init__(self):
        self.docs = []

    def add_document(self, content, source="", metadata=None, context=None):
        self.docs.append({"content": content, "source": source,
                          "metadata": metadata or {}, "context": context})
        return str(len(self.docs))


def test_ingest_short_doc_not_chunked_and_title_indexed(tmp_path):
    _write_kb(tmp_path)
    mem = _Mem()
    stats = ingest_corpus(mem, load_documents(tmp_path))
    assert stats == {"documents": 2, "chunks": 2}
    # 标题进索引：短文档的标题是最强检索信号
    assert any(d["content"].startswith("测试产品甲") for d in mem.docs)
    assert all(d["metadata"].get("category") for d in mem.docs)


def test_ingest_long_doc_gets_chunked(tmp_path):
    (tmp_path / "cat").mkdir()
    long_body = "段落内容。" * 300   # 远超 chunk_size
    (tmp_path / "cat" / "x01_长文.md").write_text(
        "# 长文档\n\n" + long_body, encoding="utf-8"
    )
    mem = _Mem()
    stats = ingest_corpus(mem, load_documents(tmp_path), chunk_size=200, overlap=40)
    assert stats["documents"] == 1
    assert stats["chunks"] > 1

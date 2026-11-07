"""知识库目录加载器。

此前语料硬编码在 evaluation/evaluator.py 里（3 篇、每篇 50-70 字），
检索指标没有区分度。本模块从 knowledge_base/ 目录扫描真实规模的语料，
带分类元数据，支持分块与 Contextual Retrieval。

目录约定：
    knowledge_base/<category>/<nn>_<slug>.md
分类取自目录名（products / accounts / service ...），
source 用相对路径（如 products/p01_稳健理财90天.md），便于按文档归因。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_KB_DIR = Path("knowledge_base")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_DISCLAIMER_RE = re.compile(r"^>\s*合成演示数据.*$", re.MULTILINE)


@dataclass
class KbDocument:
    """一篇知识库文档。"""

    source: str            # 相对路径，作为检索归因的 source
    title: str
    category: str
    content: str           # 去掉标题与免责声明后的正文
    raw: str               # 原始全文
    path: Path = field(repr=False, default=None)  # type: ignore[assignment]

    @property
    def char_count(self) -> int:
        return len(self.content)


def _parse(path: Path, root: Path) -> KbDocument:
    raw = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    category = path.relative_to(root).parts[0] if len(path.relative_to(root).parts) > 1 else "root"
    m = _TITLE_RE.search(raw)
    title = m.group(1) if m else path.stem
    body = _TITLE_RE.sub("", raw, count=1)
    body = _DISCLAIMER_RE.sub("", body)
    return KbDocument(
        source=rel, title=title.strip(), category=category,
        content=body.strip(), raw=raw, path=path,
    )


def load_documents(kb_dir: str | Path = DEFAULT_KB_DIR) -> list[KbDocument]:
    """扫描目录下所有 .md，按 source 排序返回。"""
    root = Path(kb_dir)
    if not root.exists():
        logger.warning("知识库目录不存在: %s", root)
        return []
    docs = [_parse(p, root) for p in sorted(root.rglob("*.md"))]
    logger.info("已加载知识库：%d 篇（%s）", len(docs), root)
    return docs


def corpus_stats(docs: list[KbDocument]) -> dict[str, Any]:
    """语料规模统计，用于报告。"""
    if not docs:
        return {"documents": 0}
    lengths = sorted(d.char_count for d in docs)
    by_cat: dict[str, int] = {}
    for d in docs:
        by_cat[d.category] = by_cat.get(d.category, 0) + 1
    n = len(lengths)
    return {
        "documents": n,
        "categories": by_cat,
        "total_chars": sum(lengths),
        "chars_min": lengths[0],
        "chars_p50": lengths[n // 2],
        "chars_max": lengths[-1],
        "chars_avg": sum(lengths) // n,
    }


def ingest_corpus(
    memory: Any,
    docs: list[KbDocument],
    *,
    chunk_size: int = 512,
    overlap: int = 128,
    with_context: bool = False,
    llm: Any = None,
) -> dict[str, int]:
    """把语料写入向量库。

    with_context=False：整篇入库（文档本身就在 512 字左右时更合适，
    切块反而割裂语义）；超过 chunk_size 的文档才切块。
    with_context=True：切块后用 LLM 生成上下文前缀（Contextual Retrieval）。
    返回 {"documents": n, "chunks": m}。
    """
    from memory.long_term import LongTermMemory

    chunks_written = 0
    for doc in docs:
        pieces = (
            LongTermMemory._chunk_text(doc.content, chunk_size=chunk_size, overlap=overlap)
            if doc.char_count > chunk_size
            else [doc.content]
        )
        for piece in pieces:
            # 标题进正文索引：短文档的标题往往就是最强检索信号
            indexed = f"{doc.title}\n{piece}"
            memory.add_document(
                indexed,
                source=doc.source,
                metadata={"category": doc.category, "title": doc.title},
            )
            chunks_written += 1
    return {"documents": len(docs), "chunks": chunks_written}


async def ingest_corpus_contextual(
    memory: Any,
    docs: list[KbDocument],
    llm: Any,
    *,
    chunk_size: int = 512,
    overlap: int = 128,
    limit: int | None = None,
) -> dict[str, int]:
    """带 Contextual Retrieval 的入库（每块一次 LLM 调用，成本较高）。

    limit 可限制处理的文档数，便于先小样本验证收益再全量跑。
    """
    from memory.ingest import make_context
    from memory.long_term import LongTermMemory

    subset = docs[:limit] if limit else docs
    chunks_written = 0
    for doc in subset:
        pieces = (
            LongTermMemory._chunk_text(doc.content, chunk_size=chunk_size, overlap=overlap)
            if doc.char_count > chunk_size
            else [doc.content]
        )
        for piece in pieces:
            ctx = await make_context(llm, doc.content, piece, source=doc.source)
            indexed = f"{doc.title}\n{piece}"
            memory.add_document(
                indexed,
                source=doc.source,
                metadata={"category": doc.category, "title": doc.title},
                context=ctx or None,
            )
            chunks_written += 1
    return {"documents": len(subset), "chunks": chunks_written}


__all__ = [
    "KbDocument",
    "load_documents",
    "corpus_stats",
    "ingest_corpus",
    "ingest_corpus_contextual",
    "DEFAULT_KB_DIR",
]

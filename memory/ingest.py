"""知识库写入：Contextual Retrieval（T-P2-01）。

做法（Anthropic 2024-09 提出，现已成为 RAG 提点的常见做法）：
在切分之后、入库之前，用 LLM 为每个片段生成 1-2 句"它讲的是什么、位于文档何处"的上下文，
把该上下文与片段一起向量化 / 建 BM25 索引。检索时仍返回原始片段（不污染展示内容）。

相比"直接切块入库"，它主要改善：
- 指代消解（"该产品"→ 知道是哪款产品）
- 主题漂移（片段本身缺少标题信息）
"""
from __future__ import annotations

import logging
from typing import Any

from memory.long_term import LongTermMemory

logger = logging.getLogger(__name__)

CONTEXT_PROMPT = """下面是一份客服知识库文档的片段。请用 1-2 句中文说明：
1) 该片段讨论的主题；
2) 它在整篇文档中的位置或适用条件。

文档来源：{source}
整篇文档（可能被截断）：
{doc}

需要补全上下文的片段：
{chunk}

只输出那 1-2 句话，不要复述片段内容，不要加引号或前缀。"""


async def make_context(llm: Any, doc_text: str, chunk: str, source: str = "") -> str:
    """为一个片段生成上下文说明；失败返回空串（不影响入库）。"""
    if llm is None:
        return ""
    prompt = CONTEXT_PROMPT.format(source=source or "未知", doc=doc_text[:2000], chunk=chunk[:1200])
    try:
        response = await llm.ainvoke(prompt)
    except Exception as exc:  # 上下文生成失败不应阻断入库
        logger.warning("上下文生成失败，退化为原始片段入库: %s", type(exc).__name__)
        return ""
    text = (getattr(response, "content", "") or "").strip()
    if not text or len(text) > 500:
        return text[:500]
    return text


async def ingest_text(
    memory: LongTermMemory,
    llm: Any,
    text: str,
    source: str = "",
    *,
    chunk_size: int = 512,
    overlap: int = 128,
    max_chunks: int = 200,
) -> int:
    """把一篇文档切块 + 生成上下文 + 写入长期记忆，返回写入条数。"""
    chunks = LongTermMemory._chunk_text(text, chunk_size=chunk_size, overlap=overlap)[:max_chunks]
    written = 0
    for chunk in chunks:
        context = await make_context(llm, text, chunk, source=source)
        memory.add_document(chunk, source=source, context=context or None)
        written += 1
    return written


async def ingest_documents(memory: LongTermMemory, llm: Any, docs: list[dict]) -> int:
    """批量写入：[{content, source}]，返回总条数。"""
    total = 0
    for doc in docs:
        total += await ingest_text(
            memory, llm, doc.get("content", ""), source=doc.get("source", "")
        )
    return total

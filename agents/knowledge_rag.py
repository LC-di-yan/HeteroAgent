"""
知识检索Agent — RAG知识库问答
负责从向量数据库中检索相关文档，结合上下文生成准确回答。
实现完整的RAG流程：Query改写 → 向量检索 → 重排序 → 上下文注入 → 生成回答。
支持置信度阈值：检索分数过低时自动转人工，避免幻觉回答。
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call


# ─── 置信度阈值 ───
# 向量检索分数低于此阈值时，判定为"知识库中无相关内容"，自动转人工
# 注意：使用真实 Embedding 模型时建议设为 0.3；
# 使用 hash 降级向量时分数普遍较低，阈值需下调到 0.05
RETRIEVAL_CONFIDENCE_THRESHOLD = float(os.getenv("RAG_CONFIDENCE_THRESHOLD", "0.05"))

RAG_SYSTEM_PROMPT = """你是一个专业的知识库问答Agent，负责根据检索到的文档回答用户问题。

回答规则：
1. 严格基于检索到的文档内容回答，不要编造信息
2. 如果文档中没有相关信息，明确告知用户并建议转人工
3. 回答要简洁专业，适合客服场景
4. 对于金融产品信息，必须标注"以上信息仅供参考，具体以合同条款为准"
5. 在回答末尾标注引用的文档来源

回答格式：
- 先直接回答用户问题
- 如有必要补充相关信息
- 金融场景需添加风险提示
"""

QUERY_REWRITE_PROMPT = """请将用户的口语化问题改写为更适合向量检索的查询语句。
保留核心语义，去除口语化表达，补充专业术语。
只返回改写后的查询，不要其他内容。

用户原始问题: {query}
"""


class KnowledgeRAGAgent:
    """知识检索Agent - 实现完整RAG流程"""

    def __init__(self, llm: ChatOpenAI, long_term_memory: LongTermMemory | None = None):
        self.llm = llm
        self.long_term_memory = long_term_memory or LongTermMemory()
        # ── Cross-Encoder 重排序模型（Q12，懒加载 + 降级）──
        self._cross_encoder = None
        self._cross_encoder_checked = False

    def _get_cross_encoder(self):
        """
        懒加载 Cross-Encoder 重排序模型（Q12）。
        优先使用 sentence-transformers 的 CrossEncoder；
        不可用（未安装/模型下载失败）时返回 None，降级为 LLM 重排。
        """
        if self._cross_encoder_checked:
            return self._cross_encoder
        self._cross_encoder_checked = True

        try:
            from sentence_transformers import CrossEncoder
            self._cross_encoder = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2"
            )
        except Exception:
            self._cross_encoder = None
        return self._cross_encoder

    @trace_agent_call("rag_query_rewrite")
    async def rewrite_query(self, original_query: str) -> str:
        """Query改写：将口语化问题转为检索友好的查询"""
        messages = [
            HumanMessage(content=QUERY_REWRITE_PROMPT.format(query=original_query)),
        ]
        response = await self.llm.ainvoke(messages)
        return response.content.strip()

    @trace_agent_call("rag_retrieve")
    async def retrieve_documents(self, query: str, top_k: int = 5) -> list[dict]:
        """从向量数据库检索相关文档"""
        docs = self.long_term_memory.search(query, top_k=top_k)
        return docs

    def _rerank_cross_encoder(
        self, query: str, documents: list[dict], top_k: int
    ) -> list[dict] | None:
        """
        Cross-Encoder 重排序（Q12）。
        Cross-Encoder 同时编码 query+doc 对，相关性判断远优于 Bi-Encoder 和 LLM 重排。
        返回 None 表示 Cross-Encoder 不可用，调用方降级。
        """
        ce = self._get_cross_encoder()
        if ce is None or not documents:
            return None

        pairs = [(query, doc.get("content", "")[:512]) for doc in documents]
        try:
            scores = ce.predict(pairs)
            scored = list(zip(scores, documents))
            scored.sort(key=lambda x: x[0], reverse=True)
            results = []
            for score, doc in scored[:top_k]:
                d = doc.copy()
                d["rerank_score"] = float(score)
                results.append(d)
            return results
        except Exception:
            return None

    @trace_agent_call("rag_rerank")
    async def rerank_documents(
        self, query: str, documents: list[dict], top_k: int = 3
    ) -> list[dict]:
        """
        对检索结果重排序（Q12）。

        两阶段策略：
        1. 优先 Cross-Encoder（专业重排器，毫秒级，相关性判断精准）
        2. 不可用时降级为 LLM 重排（更慢但语义理解强）
        """
        if not documents:
            return []

        # ── 策略1: Cross-Encoder 重排 ──
        ce_results = self._rerank_cross_encoder(query, documents, top_k)
        if ce_results is not None:
            return ce_results

        # ── 策略2: LLM 重排（降级）──
        doc_summaries = "\n".join(
            f"[{i}] {doc.get('content', '')[:200]}"
            for i, doc in enumerate(documents)
        )

        messages = [
            SystemMessage(content="你是一个文档相关性排序专家。"),
            HumanMessage(content=(
                f"用户查询: {query}\n\n"
                f"候选文档:\n{doc_summaries}\n\n"
                f"请返回最相关的{top_k}个文档的索引号，用逗号分隔，如: 0,2,4"
            )),
        ]

        response = await self.llm.ainvoke(messages)

        try:
            indices = [int(i.strip()) for i in response.content.split(",")]
            reranked = [documents[i] for i in indices if i < len(documents)]
        except (ValueError, IndexError):
            reranked = documents[:top_k]

        return reranked

    @trace_agent_call("rag_generate")
    async def generate_answer(self, query: str, context_docs: list[dict]) -> str:
        """基于检索文档生成回答"""
        if not context_docs:
            return "抱歉，知识库中暂未找到与您问题相关的信息。建议您联系人工客服获取帮助。"

        context = "\n\n---\n\n".join(
            f"来源: {doc.get('source', '未知')}\n内容: {doc.get('content', '')}"
            for doc in context_docs
        )

        messages = [
            SystemMessage(content=RAG_SYSTEM_PROMPT),
            HumanMessage(content=(
                f"用户问题: {query}\n\n"
                f"检索到的参考文档:\n{context}"
            )),
        ]

        response = await self.llm.ainvoke(messages)
        return response.content

    @trace_agent_call("rag_self_correct")
    async def self_correct(self, query: str, context_docs: list[dict], answer: str) -> str:
        """
        自我纠正（Q6 — Self-Correction）。

        对生成的回答做二次审视，检查三类问题：
        1. 编造（hallucination）：回答了文档中没有的信息
        2. 矛盾：回答与文档内容冲突
        3. 遗漏：文档中的关键信息没有覆盖

        发现问题时返回修正版回答；无问题则原样返回。
        """
        if not context_docs:
            return answer

        context = "\n\n---\n\n".join(
            f"来源: {doc.get('source', '未知')}\n内容: {doc.get('content', '')}"
            for doc in context_docs
        )

        correction_prompt = """你是一个严谨的答案审查员。请检查以下回答是否忠实于参考文档。

审查要求：
1. 回答中是否有文档中不存在的"编造"信息？若有，必须删除或修正
2. 回答是否与文档内容矛盾？若有，以文档为准修正
3. 回答是否遗漏了文档中的关键信息？若有，补充关键点
4. 保持回答简洁专业，不要添加文档之外的信息

输出格式：
- 如果回答没有问题，只输出"PASS"
- 如果发现问题，直接输出修正后的完整回答（不要任何解释）

用户问题: {query}

参考文档:
{context}

待审查回答:
{answer}
"""

        messages = [
            SystemMessage(content="你是一个严格的答案审查员，只依据参考文档审查和修正回答。"),
            HumanMessage(content=correction_prompt.format(
                query=query, context=context, answer=answer
            )),
        ]

        try:
            response = await self.llm.ainvoke(messages)
            corrected = response.content.strip()

            # LLM 判定无问题 → 原样返回
            if corrected.upper().startswith("PASS"):
                return answer
            return corrected
        except Exception:
            # 审查失败时保守起见返回原回答
            return answer

    @trace_agent_call("knowledge_rag_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        完整RAG流程（作为Graph节点）：
        1. Query改写
        2. 向量检索
        3. 重排序
        4. 置信度检查 — 最高分低于阈值则转人工
        5. 生成回答
        6. 自我纠正 — 二次审视回答是否忠实于文档（Q6）
        """
        messages = state.get("messages", [])
        if not messages:
            return state

        original_query = messages[-1].content

        rewritten_query = await self.rewrite_query(original_query)

        raw_docs = await self.retrieve_documents(rewritten_query, top_k=5)

        # ── 置信度阈值检查 ──
        if raw_docs:
            best_score = max(doc.get("score", 0) for doc in raw_docs)
        else:
            best_score = 0.0

        if best_score < RETRIEVAL_CONFIDENCE_THRESHOLD:
            # 检索分数过低 → 知识库中无相关内容 → 转人工
            answer = (
                f"抱歉，知识库中暂未找到与您的问题高度相关的信息"
                f"（检索置信度 {best_score:.2f}，阈值 {RETRIEVAL_CONFIDENCE_THRESHOLD}）。\n\n"
                "已自动为您转接人工客服，请稍候。"
            )
            return {
                **state,
                "sub_results": {
                    **state.get("sub_results", {}),
                    "knowledge_rag": answer,
                },
            }

        reranked_docs = await self.rerank_documents(rewritten_query, raw_docs, top_k=3)

        answer = await self.generate_answer(original_query, reranked_docs)

        # ── 自我纠正（Q6）──
        answer = await self.self_correct(original_query, reranked_docs, answer)

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "knowledge_rag": answer,
            },
        }

"""知识检索 Agent —— P0 清理版。

变化（T-P0-05 / T-P0-10 / 4.4.3）：
- 阈值来自集中配置，不再硬编码 0.05；
- 检索优先走异步 asearch，避免同步 embedding 阻塞事件循环；
- reranker 仅在显式开启时加载，且 CPU 推理放线程池 + 超时；
- 不再用 LLM 重排作为默认路径（省一次调用，分数不可比）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from config import Settings
from config import settings as default_settings
from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)

RAG_SYSTEM_PROMPT = """你是一个专业的知识库问答 Agent，负责根据检索到的文档回答用户问题。

回答规则：
1. 严格基于检索到的文档内容回答，不要编造信息
2. 如果文档中没有相关信息，明确告知用户并建议转人工
3. 回答简洁专业，适合客服场景
4. 金融产品信息必须标注"以上信息仅供参考，具体以合同条款为准"
5. 在回答末尾标注引用的文档来源
"""

QUERY_REWRITE_PROMPT = """请将用户的口语化问题改写为更适合向量检索的查询语句。
保留核心语义，去除口语化表达，补充专业术语。
只返回改写后的查询，不要其他内容。

用户原始问题: {query}
"""

CORRECTION_PROMPT = """你是严谨的答案审查员。请检查回答是否忠实于参考文档。

审查要求：
1. 回答中是否有文档中不存在的"编造"信息？若有必须删除或修正
2. 回答是否与文档冲突？以文档为准修正
3. 是否遗漏文档中的关键信息？若有补充

输出格式：
- 没有问题只输出"PASS"
- 有问题直接输出修正后的完整回答（不要解释）

用户问题: {query}

参考文档:
{context}

待审查回答:
{answer}
"""


class KnowledgeRAGAgent:
    """完整 RAG 流程：改写 → 检索 → （可选）重排 → 置信度门禁 → 生成 → 自检。"""

    def __init__(self, llm: Any, long_term_memory: Any | None = None,
                 settings: Settings = default_settings, context_store: Any | None = None,
                 semantic_cache: Any | None = None):
        self.llm = llm
        self.settings = settings
        self._memory = long_term_memory
        # 跨会话用户事实（T-P2-02）：例如"该用户近期提交过退款工单 TK-..."
        self.context_store = context_store
        # 语义缓存（T-P3-04）：只缓存非个性化回答，按租户隔离
        self.semantic_cache = semantic_cache
        self._cross_encoder = None
        self._cross_encoder_checked = False
        self._cross_encoder_lock = asyncio.Lock()

    # ── 记忆懒加载：允许在 lifespan 之外注入 ──
    @property
    def memory(self):
        if self._memory is None:
            from memory.long_term import create_vector_store

            try:
                self._memory = create_vector_store(settings=self.settings)
            except TypeError:
                self._memory = create_vector_store()
        return self._memory

    # ── 重排 ──

    async def _get_cross_encoder(self):
        if self._cross_encoder_checked:
            return self._cross_encoder
        async with self._cross_encoder_lock:
            if self._cross_encoder_checked:
                return self._cross_encoder
            self._cross_encoder_checked = True
            if not self.settings.rerank_enabled:
                return None
            try:
                from sentence_transformers import CrossEncoder

                self._cross_encoder = await asyncio.to_thread(
                    CrossEncoder, self.settings.rerank_model
                )
            except Exception as exc:  # 未安装/下载失败/离线
                logger.warning("reranker 不可用，跳过重排: %s", exc)
                self._cross_encoder = None
        return self._cross_encoder

    async def _rerank(self, query: str, documents: list[dict], top_k: int) -> list[dict]:
        if not documents:
            return []
        ce = await self._get_cross_encoder()
        if ce is None:
            return documents[:top_k]
        pairs = [(query, (doc.get("content") or "")[:512]) for doc in documents]
        try:
            scores = await asyncio.wait_for(
                asyncio.to_thread(ce.predict, pairs), timeout=10.0
            )
        except Exception as exc:
            logger.warning("重排失败，保持原序: %s", exc)
            return documents[:top_k]
        ranked = sorted(zip(scores, documents, strict=True), key=lambda item: float(item[0]), reverse=True)
        out = []
        for score, doc in ranked[:top_k]:
            enriched = dict(doc)
            enriched["rerank_score"] = float(score)
            out.append(enriched)
        return out

    # ── 检索 ──

    async def _retrieve(self, query: str, top_k: int) -> list[dict]:
        memory = self.memory
        try:
            if hasattr(memory, "asearch"):
                return await memory.asearch(query, top_k=top_k)
        except Exception as exc:
            if type(exc).__name__ == "EmbeddingUnavailableError":
                raise
            logger.warning("异步检索失败，回退线程池: %s", exc)
        return await asyncio.to_thread(memory.search, query, top_k)

    # ── 生成 ──

    @trace_agent_call("rag_query_rewrite")
    async def rewrite_query(self, original_query: str) -> str:
        try:
            response = await self.llm.ainvoke(
                [HumanMessage(content=QUERY_REWRITE_PROMPT.format(query=original_query))]
            )
            text = (getattr(response, "content", "") or "").strip()
            return text or original_query
        except Exception:
            return original_query

    async def _cache_embed(self, text: str) -> list[float] | None:
        """缓存用的 embedding：优先异步接口，其次线程池跑同步实现，失败返回 None。"""
        memory = self.memory
        aembed = getattr(memory, "aembed", None)
        if callable(aembed):
            try:
                return await aembed(text)
            except Exception:
                return None
        sync_embed = getattr(memory, "_embed_sync", None)
        if callable(sync_embed):
            try:
                return await asyncio.to_thread(sync_embed, text)
            except Exception:
                return None
        return None

    def _kb_version(self) -> str:
        docs = getattr(self.memory, "_documents", None)
        return str(len(docs)) if docs is not None else "0"

    def _user_context(self, state: dict[str, Any]) -> str:
        if self.context_store is None:
            return ""
        try:
            return self.context_store.format_for_prompt(str(state.get("user_id") or "anonymous"))
        except Exception as exc:  # 记忆读取失败不影响回答
            logger.warning("用户事实读取失败: %s", type(exc).__name__)
            return ""

    @trace_agent_call("rag_generate")
    async def generate_answer(self, query: str, context_docs: list[dict],
                              user_context: str = "") -> str:
        if not context_docs:
            return "抱歉，知识库中暂未找到与您问题相关的信息。建议您联系人工客服获取帮助。"
        context = "\n\n---\n\n".join(
            f"来源: {doc.get('source', '未知')}\n内容: {doc.get('content', '')}"
            for doc in context_docs
        )
        human = f"用户问题: {query}\n\n检索到的参考文档:\n{context}"
        if user_context:
            human = f"{user_context}\n\n{human}"
        # tags 用于流式输出筛选：只有最终答案的 token 才推给前端，
        # 查询改写/自检的中间 token 不应出现在用户界面上（T-P2-06）。
        response = await self.llm.ainvoke(
            [SystemMessage(content=RAG_SYSTEM_PROMPT), HumanMessage(content=human)],
            config={"tags": ["final_answer"]},
        )
        return (getattr(response, "content", "") or "").strip()

    @trace_agent_call("rag_self_correct")
    async def self_correct(self, query: str, context_docs: list[dict], answer: str) -> str:
        if not context_docs:
            return answer
        context = "\n\n---\n\n".join(
            f"来源: {doc.get('source', '未知')}\n内容: {doc.get('content', '')}"
            for doc in context_docs
        )
        try:
            response = await self.llm.ainvoke([
                SystemMessage(content="你是严格的答案审查员，只依据参考文档审查和修正回答。"),
                HumanMessage(content=CORRECTION_PROMPT.format(
                    query=query, context=context, answer=answer)),
            ])
            corrected = (getattr(response, "content", "") or "").strip()
            if corrected.upper().startswith("PASS"):
                return answer
            return corrected or answer
        except Exception:
            return answer

    @trace_agent_call("knowledge_rag_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages") or []
        if not messages:
            return state

        original_query = getattr(messages[-1], "content", "") or ""
        user_context = self._user_context(state)
        tenant = str(state.get("tenant_id") or "default")

        # 语义缓存：只对"非个性化"问题生效（带用户事实的请求既不读也不写缓存）
        cache = self.semantic_cache
        cache_usable = cache is not None and not user_context

        if cache_usable:
            try:
                hit = await cache.lookup(
                    original_query, tenant=tenant, kb_version=self._kb_version(),
                    embed=self._cache_embed,
                )
            except Exception as exc:  # 缓存故障不应影响主链路
                logger.warning("语义缓存查询失败: %s", type(exc).__name__)
                hit = None
            if hit is not None:
                logger.info("语义缓存命中（%s, sim=%.3f）", hit.match, hit.similarity)
                return {
                    **state,
                    "sub_results": {
                        **state.get("sub_results", {}),
                        "knowledge_rag": hit.answer,
                        "cache": {"hit": True, "match": hit.match,
                                  "similarity": round(hit.similarity, 4)},
                    },
                }

        try:
            rewritten = await self.rewrite_query(original_query)
            raw_docs = await self._retrieve(rewritten, top_k=self.settings.rag_top_k)
        except Exception as exc:
            logger.error("检索失败: %s", exc)
            answer = ("抱歉，知识检索服务暂时不可用，已记录您的问题。"
                      "请稍后重试或联系人工客服。")
            return {**state, "sub_results": {**state.get("sub_results", {}), "knowledge_rag": answer}}

        best_score = max((doc.get("score", 0.0) for doc in raw_docs), default=0.0)
        threshold = float(self.settings.rag_confidence_threshold)
        if not raw_docs or best_score < threshold:
            answer = (
                "抱歉，知识库中暂未找到与您问题高度相关的信息。"
                "已为您转接人工客服，请稍候。"
            )
            return {**state, "sub_results": {**state.get("sub_results", {}), "knowledge_rag": answer}}

        docs = await self._rerank(rewritten, raw_docs, top_k=self.settings.rag_rerank_top_k)
        answer = await self.generate_answer(
            original_query, docs, user_context=user_context
        )
        answer = await self.self_correct(original_query, docs, answer)

        if cache_usable and answer:
            try:
                await self.semantic_cache.store(
                    original_query, answer, tenant=tenant, kb_version=self._kb_version(),
                    sources=[str(d.get("source", "")) for d in docs], embed=self._cache_embed,
                )
            except Exception as exc:
                logger.warning("语义缓存写入失败: %s", type(exc).__name__)
        return {**state, "sub_results": {**state.get("sub_results", {}), "knowledge_rag": answer}}

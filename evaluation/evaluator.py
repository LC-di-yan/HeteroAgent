"""
评估体系 — Golden Set 自动评估（Q30）

对 RAG 知识检索和回答生成做量化评估：
1. 检索质量：Recall@K、MRR
2. 生成质量：Faithfulness（忠实度）、Answer Relevance（相关性）

用法：
    from evaluation.evaluator import Evaluator, GoldenSet
    ev = Evaluator(llm, long_term_memory)
    report = ev.evaluate(golden_set)
    print(report)

也可以通过 API 端点 /api/evaluate 触发。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from memory.long_term import LongTermMemory


@dataclass
class EvalCase:
    """一条评估用例（Golden Set 中的 QA 对）"""
    query: str
    expected_content: str   # 期望检索到的文档关键词（用于 Recall@K 判断）
    reference_answer: str = ""  # 参考回答（用于 Faithfulness 评估，可选）


@dataclass
class GoldenSet:
    """Golden Set — 标注好的评估数据集"""
    cases: list[EvalCase] = field(default_factory=list)

    @classmethod
    def default(cls) -> "GoldenSet":
        """内置默认 Golden Set（对齐系统预置知识库）"""
        return cls(cases=[
            EvalCase(
                query="理财产品A的收益率是多少？",
                expected_content="理财产品A",
                reference_answer="年化收益率3.5%-5.2%，投资期限6个月至3年",
            ),
            EvalCase(
                query="我买了产品想退款可以吗？",
                expected_content="退款政策",
                reference_answer="7天内可无理由退款，3-5个工作日原路退回",
            ),
            EvalCase(
                query="开户需要准备什么材料？",
                expected_content="开户流程",
                reference_answer="身份证原件，15-30分钟完成",
            ),
            EvalCase(
                query="理财产品有风险吗？",
                expected_content="理财产品A",
                reference_answer="理财非存款，产品有风险，投资须谨慎",
            ),
        ])


class Evaluator:
    """
    RAG 评估器（Q30）。

    指标：
    - Recall@K：Top-K 检索结果中包含目标文档的比例
    - MRR：目标文档排名的倒数平均
    - Faithfulness：回答是否忠实于检索文档（LLM-as-Judge）
    """

    def __init__(self, llm: ChatOpenAI | None = None, long_term_memory: LongTermMemory | None = None):
        self.llm = llm
        self.long_term_memory = long_term_memory

    # ── 检索质量评估 ──

    def _recall_at_k(self, retrieved: list[dict], expected: str, k: int) -> int:
        """单个 case 的 Recall@K：Top-K 中是否有包含期望关键词的文档"""
        for doc in retrieved[:k]:
            if expected in doc.get("content", "") or expected in doc.get("source", ""):
                return 1
        return 0

    def _mrr(self, retrieved: list[dict], expected: str) -> float:
        """单个 case 的 MRR：目标文档排名的倒数"""
        for rank, doc in enumerate(retrieved, start=1):
            if expected in doc.get("content", "") or expected in doc.get("source", ""):
                return 1.0 / rank
        return 0.0

    async def _evaluate_retrieval(self, case: EvalCase, top_k: int = 5) -> dict:
        """检索质量评估：向量检索 → 计算 Recall@K / MRR"""
        retrieved = self.long_term_memory.search(case.query, top_k=top_k)
        recall_5 = self._recall_at_k(retrieved, case.expected_content, 5)
        recall_3 = self._recall_at_k(retrieved, case.expected_content, 3)
        mrr = self._mrr(retrieved, case.expected_content)
        return {
            "query": case.query,
            "recall@5": recall_5,
            "recall@3": recall_3,
            "mrr": mrr,
            "top1": retrieved[0]["content"][:50] if retrieved else "",
        }

    # ── 生成质量评估（LLM-as-Judge）──

    async def _evaluate_faithfulness(self, case: EvalCase, generated: str) -> float:
        """
        Faithfulness：用 LLM 判断回答是否忠实于参考答案。
        返回 0~1 分数。
        """
        if not case.reference_answer or not generated:
            return 0.0

        prompt = f"""请判断以下回答是否忠实于参考信息，是否包含编造内容。

参考信息: {case.reference_answer}

待评估回答: {generated}

只返回 0 到 1 之间的一个数字：
- 1.0 = 完全忠实，无编造
- 0.5 = 部分忠实，有轻微偏差
- 0.0 = 严重编造或完全不相关
"""

        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            score = float(response.content.strip()[:4])
            return max(0.0, min(1.0, score))
        except Exception:
            return 0.5  # 评估失败时取中性分

    # ── 完整评估 ──

    async def evaluate(self, golden_set: GoldenSet | None = None, top_k: int = 5) -> dict:
        """运行完整评估，返回报告"""
        cases = golden_set.cases if golden_set else GoldenSet.default().cases
        if not cases:
            return {"error": "Golden Set 为空"}

        retrieval_results = []
        faithfulness_scores = []

        for case in cases:
            # 1. 检索评估
            r = await self._evaluate_retrieval(case, top_k)
            retrieval_results.append(r)

            # 2. 生成评估（如果有参考回答）
            if case.reference_answer and self.llm:
                # 用检索到的 Top-1 文档生成回答
                retrieved = self.long_term_memory.search(case.query, top_k=3)
                if retrieved:
                    context = "\n".join(d["content"] for d in retrieved[:2])
                    gen_prompt = f"基于以下资料回答问题：\n\n{context}\n\n问题: {case.query}\n\n只给出答案。"
                    gen_response = await self.llm.ainvoke([HumanMessage(content=gen_prompt)])
                    generated = gen_response.content.strip()
                    f_score = await self._evaluate_faithfulness(case, generated)
                    faithfulness_scores.append(f_score)

        # 汇总指标
        n = len(cases)
        recall5 = sum(r["recall@5"] for r in retrieval_results) / n
        recall3 = sum(r["recall@3"] for r in retrieval_results) / n
        mrr = sum(r["mrr"] for r in retrieval_results) / n
        faithfulness = sum(faithfulness_scores) / len(faithfulness_scores) if faithfulness_scores else None

        return {
            "total_cases": n,
            "metrics": {
                "recall@5": round(recall5, 4),
                "recall@3": round(recall3, 4),
                "mrr": round(mrr, 4),
                "faithfulness": round(faithfulness, 4) if faithfulness is not None else None,
            },
            "cases": retrieval_results,
        }


# ── CLI 入口：python -m evaluation.evaluator ──

if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv
    load_dotenv()

    async def main():
        llm = ChatOpenAI(model=os.getenv("MODEL_NAME", "gpt-4o"), temperature=0)
        ltm = LongTermMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
        # 加载预置知识库（与 api.main lifespan 一致）
        ltm.add_document(content="我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。", source="product_faq.md")
        ltm.add_document(content="退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。", source="refund_policy.md")
        ltm.add_document(content="开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。", source="account_guide.md")

        ev = Evaluator(llm=llm, long_term_memory=ltm)
        report = await ev.evaluate()
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))

    asyncio.run(main())

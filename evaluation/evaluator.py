"""评估体系 — 版本化 Golden Set + RAG 离线评估（T-P2-04 离线部分 / T-P0-09 数据集基线）。

数据集（可版本化，不再硬编码在代码里）
--------------------------------------
`evaluation/datasets/v1.jsonl`，每行一个独立 JSON 对象（UTF-8 无 BOM）:

    {"id": "qa-001",
     "query": "理财产品A的年化收益率是多少？",
     "expected_source": "product_faq.md" | "refund_policy.md"
                        | "account_guide.md" | "conversation_general" | "out_of_scope",
     "expected_keywords": ["3.5%-5.2%", "年化收益率"],
     "reference_answer": "……",
     "tags": ["faq", "product", "yield"]}

用例分层与指标分母
------------------
* grounded（`expected_source` 为三篇知识文档之一）
  → 计入 Recall@3 / Recall@5 / MRR / nDCG@5。
* `out_of_scope`（知识库没有的内容）
  → 计入 coverage：检索最高分低于 `confidence_threshold` 即判"未命中→转人工"正确。
* `conversation_general`（寒暄/能力介绍/投诉/转人工）
  → 无金标签文档，不参与检索分母，只出现在逐条明细与 judge 类指标里。
* faithfulness / answer_relevancy
  → LLM-as-Judge（Pydantic `JudgeScore`）；判分失败只累加
    `judge_failures`，**不再默默给中性 0.5**，也不计入均值。

知识库语料（与 `api/main.py` lifespan 预置完全一致；调用方可直接加载
`KNOWLEDGE_BASE_CORPUS` 或调用 `load_corpus(memory)`）
------------------------------------------------------------------
product_faq.md:
    我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。
    注意：理财非存款，产品有风险，投资须谨慎。
refund_policy.md:
    退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。
    退款将在3-5个工作日内原路退回。
account_guide.md:
    开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码
    5.完成风险评估问卷。整个流程约需15-30分钟。

用法
----
    from evaluation.evaluator import Evaluator, GoldenSet

    ev = Evaluator(llm, long_term_memory)
    report = await ev.evaluate(GoldenSet.default())
    # report = {"total_cases", "metrics", "judge_failures", "cases", ...}

CLI（离线可跑；无 LLM 时 faithfulness/answer_relevancy 为 None）::

    python -m evaluation.evaluator --dataset evaluation/datasets/v1.jsonl
    python -m evaluation.evaluator --no-llm --memory stub --confidence-threshold 0.3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

if TYPE_CHECKING:  # 仅类型标注：离线环境不强制依赖这些包
    from langchain_openai import ChatOpenAI

__all__ = [
    "JudgeScore",
    "EvalCase",
    "GoldenSet",
    "Evaluator",
    "StubKnowledgeMemory",
    "KNOWLEDGE_BASE_CORPUS",
    "DEFAULT_DATASET_PATH",
    "DEFAULT_TOP_K",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "parse_judge_score",
    "strip_code_fences",
    "dcg_at_k",
    "ndcg_at_k",
    "load_corpus",
    "main",
]

# ── 常量 ──────────────────────────────────────────────────────────────

DEFAULT_TOP_K = 5
DEFAULT_CONFIDENCE_THRESHOLD = 0.3
METRIC_ROUND_DIGITS = 4

#: 数据集默认路径（模块相对，不受当前工作目录影响）
DEFAULT_DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "v1.jsonl"

KB_SOURCES = ("product_faq.md", "refund_policy.md", "account_guide.md")
CONVERSATION_SOURCE = "conversation_general"
OUT_OF_SCOPE_SOURCE = "out_of_scope"
ALLOWED_SOURCES = KB_SOURCES + (CONVERSATION_SOURCE, OUT_OF_SCOPE_SOURCE)

#: 评估用知识库语料：与系统预置一致，供调用方加载
KNOWLEDGE_BASE_CORPUS: dict[str, str] = {
    "product_faq.md": (
        "我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，"
        "最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。"
    ),
    "refund_policy.md": (
        "退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。"
        "退款将在3-5个工作日内原路退回。"
    ),
    "account_guide.md": (
        "开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 "
        "5.完成风险评估问卷。整个流程约需15-30分钟。"
    ),
}

#: 文件缺失时 GoldenSet.default() 的回退用例（保留原内置 4 条的语义）
BUILTIN_EVAL_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "builtin-001",
        "query": "理财产品A的收益率是多少？",
        "expected_source": "product_faq.md",
        "expected_keywords": ["理财产品A", "年化收益率"],
        "reference_answer": "年化收益率3.5%-5.2%，投资期限6个月至3年",
        "tags": ["faq", "product", "yield"],
    },
    {
        "id": "builtin-002",
        "query": "我买了产品想退款可以吗？",
        "expected_source": "refund_policy.md",
        "expected_keywords": ["退款政策", "无理由退款"],
        "reference_answer": "7天内可无理由退款，3-5个工作日原路退回",
        "tags": ["faq", "refund", "policy"],
    },
    {
        "id": "builtin-003",
        "query": "开户需要准备什么材料？",
        "expected_source": "account_guide.md",
        "expected_keywords": ["开户流程", "身份证原件"],
        "reference_answer": "身份证原件，15-30分钟完成",
        "tags": ["faq", "account", "material"],
    },
    {
        "id": "builtin-004",
        "query": "理财产品有风险吗？",
        "expected_source": "product_faq.md",
        "expected_keywords": ["理财产品A", "理财非存款"],
        "reference_answer": "理财非存款，产品有风险，投资须谨慎",
        "tags": ["faq", "product", "risk", "compliance"],
    },
)

FAITHFULNESS_PROMPT = """你是严谨的评估员。请判断"待评估回答"是否忠实于"参考信息"，
即回答中的事实是否都能在参考信息里找到依据，是否存在编造或矛盾。

评分口径:
1.0 = 完全忠实，无编造、无矛盾
0.7 = 基本忠实，有轻微遗漏或表述偏差
0.4 = 部分忠实，含参考信息未支持的推断
0.0 = 严重编造或与参考信息矛盾

参考信息: {reference}

待评估回答: {answer}

请返回 score（0~1 之间的浮点数）与 reason（不超过 60 字的判定理由）。"""

RELEVANCY_PROMPT = """你是严谨的评估员。请判断"待评估回答"是否切题，
即是否直接回答了"用户问题"；答非所问、遗漏核心诉求都要扣分。

评分口径:
1.0 = 完全切题，直接回应问题
0.7 = 基本切题，有冗余或次要遗漏
0.4 = 部分切题，未答到核心诉求
0.0 = 答非所问

用户问题: {query}

待评估回答: {answer}

请返回 score（0~1 之间的浮点数）与 reason（不超过 60 字的判定理由）。"""


# ── LLM-as-Judge 结构化模型 ──────────────────────────────────────────


class JudgeScore(BaseModel):
    """LLM-as-Judge 的结构化判分结果（替代脆弱的 float(content[:4])）。"""

    score: float = Field(ge=0, le=1, description="0~1 的判分，1 为最好")
    reason: str = Field(description="判定理由（简短中文）")


# ── 文本解析兜底 ─────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^[ \t]*```[a-zA-Z0-9_+\-]*[ \t]*$|[ \t]*```[a-zA-Z0-9_+\-]*")
_FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def strip_code_fences(text: str) -> str:
    """剥掉代码围栏标记（三反引号及语言名），保留围栏内的正文。"""
    return _FENCE_RE.sub("", text or "")


def parse_judge_score(text: Any) -> float | None:
    """
    从 judge 自由文本里提取分数：剥代码围栏 → 正则取首个落在 [0,1] 的浮点数。

    取不到合法分数时返回 None（调用方计入 judge_failures）。
    刻意不再回退成 0.5：静默中性分会把"评估器坏了"伪装成"系统表现平庸"。
    """
    if text is None:
        return None
    cleaned = strip_code_fences(text if isinstance(text, str) else str(text))
    for match in _FLOAT_RE.finditer(cleaned):
        try:
            value = float(match.group())
        except ValueError:  # pragma: no cover - 正则已保证可解析
            continue
        if 0.0 <= value <= 1.0:
            return value
    return None


def extract_response_text(response: Any) -> str | None:
    """把 LangChain message / 字符串 / content-block 列表统一取成纯文本。"""
    if response is None:
        return None
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                piece = block.get("text") or block.get("content")
                if isinstance(piece, str):
                    parts.append(piece)
        return "".join(parts) if parts else None
    if content is None:
        return str(response)
    return None


# ── 检索指标（纯函数，便于单测对表） ──────────────────────────────────


def dcg_at_k(relevances: list[float], k: int) -> float:
    """DCG@k = sum(rel_i / log2(i + 1))，i 从 1 开始。"""
    return sum(rel / math.log2(i + 1) for i, rel in enumerate(relevances[:k], start=1))


def ndcg_at_k(relevances: list[float], k: int) -> float:
    """
    nDCG@k = DCG@k / IDCG@k。

    IDCG 用同一组相关性标签的理想降序排列（二值相关即"命中全部排前面"）；
    没有任何命中时 IDCG 为 0，此时 nDCG 记 0.0。
    """
    idcg = dcg_at_k(sorted(relevances, reverse=True), k)
    if idcg <= 0:
        return 0.0
    return dcg_at_k(relevances, k) / idcg


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, METRIC_ROUND_DIGITS)


def _doc_score(doc: dict) -> float:
    """检索分数：兼容 score / rrf_score / rerank_score / similarity（缺失记 0.0）。"""
    for key in ("score", "rrf_score", "rerank_score", "similarity"):
        raw = doc.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
    return 0.0


def _source_matches(expected: str, actual: str) -> bool:
    """source 比对：忽略大小写与目录前缀（docs/product_faq.md 等同 product_faq.md）。"""
    if not expected or not actual:
        return False
    e = expected.strip().lower().replace("\\", "/")
    a = actual.strip().lower().replace("\\", "/")
    if e == a or e == Path(a).name or Path(e).name == a or Path(e).name == Path(a).name:
        return True
    return a.startswith(e) or e in a


# ── 数据集结构 ────────────────────────────────────────────────────────


@dataclass
class EvalCase:
    """
    一条评估用例。

    前三个字段与旧签名完全兼容（仍可 EvalCase(query, expected_content) 位置构造）；
    id/expected_source/expected_keywords/tags 为 v1.jsonl 新增字段。
    """

    query: str
    expected_content: str  # 期望命中的关键词（旧字段，仍参与命中判定）
    reference_answer: str = ""  # 参考回答（faithfulness 判分依据）
    id: str = ""
    expected_source: str = ""  # KB_SOURCES / conversation_general / out_of_scope
    expected_keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def is_out_of_scope(self) -> bool:
        return self.expected_source == OUT_OF_SCOPE_SOURCE

    @property
    def is_conversational(self) -> bool:
        return self.expected_source == CONVERSATION_SOURCE

    @property
    def is_grounded(self) -> bool:
        """是否存在"应当被检索到"的金标签（决定它是否进入检索指标分母）。"""
        if self.expected_source in KB_SOURCES:
            return True
        if self.expected_source in (CONVERSATION_SOURCE, OUT_OF_SCOPE_SOURCE):
            return False
        # knowledge_base 相对路径（products/p01_xxx.md）视为有金标签
        if self.expected_source.endswith(".md") and "/" in self.expected_source:
            return True
        return bool(self.expected_keywords or self.expected_content)


@dataclass
class GoldenSet:
    """Golden Set — 版本化评估数据集（JSONL 落盘）。"""

    cases: list[EvalCase] = field(default_factory=list)
    source: str = ""  # 来源标记（jsonl 路径或 builtin），便于回归报告追溯

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):
        return iter(self.cases)

    # ── 加载 ──

    @classmethod
    def load(cls, path: str | Path) -> GoldenSet:
        """
        从 JSONL 读取数据集。

        每行必须是独立 JSON 对象；缺 query 或 JSON 非法都直接报错——
        静默跳过坏行会让数据集悄悄缩水，指标随之失真。
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"评估数据集不存在: {file_path}")

        raw = file_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM 会让首行 json.loads 失败
            raise ValueError(f"数据集文件含 UTF-8 BOM，请存为 UTF-8 无 BOM: {file_path}")

        cases: list[EvalCase] = []
        for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue  # 允许空行 / 结尾换行
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"数据集第 {line_no} 行不是合法 JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"数据集第 {line_no} 行不是 JSON 对象")
            cases.append(cls._case_from_obj(obj, line_no))

        if not cases:
            raise ValueError(f"数据集为空: {file_path}")
        return cls(cases=cases, source=str(file_path))

    @staticmethod
    def _case_from_obj(obj: dict, line_no: int = 0) -> EvalCase:
        query = str(obj.get("query", "") or "").strip()
        if not query:
            raise ValueError(f"数据集第 {line_no} 行缺少 query 字段")
        expected_source = str(obj.get("expected_source", "") or "").strip()
        # 除内置 3 篇与两个特殊标签外，允许 knowledge_base/ 下的相对路径
        # （形如 products/p01_xxx.md），以支持大规模语料评估。
        is_kb_path = expected_source.endswith(".md") and "/" in expected_source
        if expected_source and expected_source not in ALLOWED_SOURCES and not is_kb_path:
            raise ValueError(
                f"数据集第 {line_no} 行 expected_source 非法: {expected_source}"
                f"（允许: {', '.join(ALLOWED_SOURCES)} 或 knowledge_base 相对路径 *.md）"
            )
        keywords = obj.get("expected_keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list):
            raise ValueError(f"数据集第 {line_no} 行 expected_keywords 必须是数组")
        tags = obj.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            raise ValueError(f"数据集第 {line_no} 行 tags 必须是数组")
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
        expected_content = str(obj.get("expected_content", "") or "").strip()
        return EvalCase(
            query=query,
            # 向后兼容：旧字段缺失时用首个关键词填充 expected_content
            expected_content=expected_content or (keywords[0] if keywords else ""),
            reference_answer=str(obj.get("reference_answer", "") or ""),
            id=str(obj.get("id", "") or f"case-{line_no:03d}"),
            expected_source=expected_source,
            expected_keywords=keywords,
            tags=[str(t) for t in tags],
        )

    @classmethod
    def default(cls) -> GoldenSet:
        """
        默认 Golden Set：优先读 evaluation/datasets/v1.jsonl；
        文件缺失或不可解析时回退内置 4 条用例（保证旧链路 / GET /api/evaluate 不炸）。
        """
        path = DEFAULT_DATASET_PATH
        try:
            return cls.load(path)
        except Exception as exc:  # noqa: BLE001 - 回退路径必须无条件可用
            if os.environ.get("EVAL_DEBUG"):
                print(f"[evaluator] 数据集加载失败，回退内置用例: {exc}", file=sys.stderr)
            cases = [cls._case_from_obj(dict(obj)) for obj in BUILTIN_EVAL_CASES]
            return cls(cases=cases, source="builtin")


# ── 离线检索替身（无 numpy/faiss/网络时 CLI 仍可跑通） ─────────────────


class StubKnowledgeMemory:
    """
    确定性关键词检索替身，**仅用于离线演示与冒烟**，不代表生产检索质量。

    与 LongTermMemory 的评估侧接口一致：search(query, top_k=k) 同步返回
    [{"id", "content", "source", "score"}, ...]，score 归一化在 0~1。
    """

    def __init__(self, corpus: dict[str, str] | None = None) -> None:
        # 只有 corpus is None 才用默认语料：显式传空 dict 表示「空库」，不能被 or 吞掉
        self.corpus = dict(KNOWLEDGE_BASE_CORPUS if corpus is None else corpus)
        self._docs = [
            {"id": f"doc-{i}", "source": source, "content": content}
            for i, (source, content) in enumerate(self.corpus.items())
        ]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", (text or "").lower()))

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
        q_tokens = self._tokenize(query)
        scored: list[dict] = []
        for doc in self._docs:
            d_tokens = self._tokenize(doc["content"]) | self._tokenize(doc["source"])
            if not q_tokens or not d_tokens:
                continue
            overlap = len(q_tokens & d_tokens) / len(q_tokens)
            if overlap <= 0:
                continue
            item = dict(doc)
            item["score"] = round(overlap, METRIC_ROUND_DIGITS)
            scored.append(item)
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[: max(0, int(top_k))]

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        """与 LongTermMemory 对齐的写入接口，供 load_corpus 使用。"""
        doc_id = f"doc-{len(self._docs)}"
        self._docs.append({"id": doc_id, "source": source, "content": content})
        return doc_id


def load_corpus(memory: Any, corpus: dict[str, str] | None = None) -> int:
    """把预置语料写入任意记忆对象（需实现 add_document(content=..., source=...)）。"""
    count = 0
    for source, content in (corpus or KNOWLEDGE_BASE_CORPUS).items():
        memory.add_document(content=content, source=source)
        count += 1
    return count


# ── 评估器 ────────────────────────────────────────────────────────────


class Evaluator:
    """
    RAG 离线评估器（T-P2-04）。

    指标公式
    --------
    - recall@k：grounded 用例中 Top-k 内出现命中的比例（k = 3 / 5）
    - mrr：mean(1 / first_hit_rank)，无命中记 0
    - ndcg@5：DCG@5 / IDCG@5，DCG@k = sum(rel_i / log2(i+1))，rel 为二值命中
    - faithfulness / answer_relevancy：JudgeScore 判分均值（只对成功判分的用例取均值）
    - coverage：out_of_scope 用例中 max(retrieval_score) < confidence_threshold 的比例

    命中定义：doc["source"] 匹配 expected_source，或 expected_keywords 任一命中
    doc["content"]（旧用例的 expected_content 亦参与判定）。

    对记忆只依赖一个接口：long_term_memory.search(query, top_k=k)（同步）。
    """

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        long_term_memory: Any | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self.llm = llm
        self.long_term_memory = long_term_memory
        self.top_k = top_k
        # 运行期计数（每次 evaluate 起始重置）
        self.judge_failures = 0
        self.judge_skipped = 0

    # ── 检索 ──

    def _retrieve(self, query: str, top_k: int) -> list[dict]:
        """只调用 search(query, top_k=...)，返回统一 dict 列表。"""
        if self.long_term_memory is None:
            raise ValueError("Evaluator 需要 long_term_memory（实现 search(query, top_k)）")
        fetched = self.long_term_memory.search(query, top_k=top_k) or []
        docs: list[dict] = []
        for item in fetched:
            if isinstance(item, dict):
                docs.append(item)
            else:  # 容错：非 dict 结果也给出可判定结构
                docs.append({"content": str(item), "source": "", "score": 0.0})
        return docs

    @staticmethod
    def _is_relevant(case: EvalCase, doc: dict) -> bool:
        content = str(doc.get("content", "") or "")
        source = str(doc.get("source", "") or "")
        if _source_matches(case.expected_source, source):
            return True
        for keyword in case.expected_keywords:
            if keyword and keyword in content:
                return True
        if case.expected_content and (
            case.expected_content in content or _source_matches(case.expected_content, source)
        ):
            return True
        return False

    def _retrieval_detail(self, case: EvalCase, docs: list[dict]) -> dict:
        relevances = [1.0 if self._is_relevant(case, doc) else 0.0 for doc in docs]
        hit_ranks = [i for i, rel in enumerate(relevances, start=1) if rel > 0]
        return {
            "hit_ranks": hit_ranks,
            "recall@3": 1 if any(rank <= 3 for rank in hit_ranks) else 0,
            "recall@5": 1 if any(rank <= 5 for rank in hit_ranks) else 0,
            "mrr": 1.0 / hit_ranks[0] if hit_ranks else 0.0,
            "ndcg@5": ndcg_at_k(relevances, 5),
            "retrieved": len(docs),
            "best_score": _round(max((_doc_score(d) for d in docs), default=0.0)),
            "top1": str(docs[0].get("content", ""))[:50] if docs else "",
            "top1_source": str(docs[0].get("source", "")) if docs else "",
        }

    # ── LLM-as-Judge ──

    def _judge_runnable(self):
        """优先结构化输出；模型不支持 with_structured_output 时返回 None（AttributeError）。"""
        if self.llm is None:
            return None
        try:
            return self.llm.with_structured_output(JudgeScore)
        except AttributeError:
            return None

    @classmethod
    def _coerce_judge(cls, result: Any) -> tuple[float | None, str]:
        """把 JudgeScore / dict / message 对象统一收敛成 (score, reason)；拿不到合法分数返回 None。"""
        if isinstance(result, JudgeScore):
            return float(result.score), str(result.reason)
        if isinstance(result, BaseModel):
            return cls._coerce_judge(result.model_dump())
        if isinstance(result, dict):
            reason = str(result.get("reason", "") or "")
            raw = result.get("score")
            if isinstance(raw, bool):
                return None, reason
            if isinstance(raw, (int, float)) and 0.0 <= float(raw) <= 1.0:
                return float(raw), reason
            if isinstance(raw, str):
                return parse_judge_score(raw), reason
            return None, reason
        if isinstance(result, (int, float)) and not isinstance(result, bool):
            value = float(result)
            return (value if 0.0 <= value <= 1.0 else None), ""
        text = extract_response_text(result)
        if text is None:
            return None, ""
        return parse_judge_score(text), text.strip()[:120]

    async def _judge(self, prompt: str) -> tuple[float | None, str]:
        """
        单问判分：结构化输出优先 → 失败回退 ainvoke + 文本解析。

        返回 (score_or_None, reason)；score 为 None 即判分失败，由调用方计数。
        """
        messages = [
            SystemMessage(content="你是严格的 RAG 质量评估员，只输出评分与简短理由。"),
            HumanMessage(content=prompt),
        ]

        runnable = self._judge_runnable()
        if runnable is not None:
            try:
                score, reason = self._coerce_judge(await runnable.ainvoke(messages))
            except AttributeError:
                score, reason = None, "structured_runnable_not_async"
            except Exception as exc:  # noqa: BLE001 - 结构化失败可降级，但必须留痕
                score, reason = None, f"structured_output_failed:{type(exc).__name__}"
            if score is not None:
                return score, reason

        try:
            response = await self.llm.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            return None, f"llm_call_failed:{type(exc).__name__}"
        text = extract_response_text(response)
        return parse_judge_score(text), (text or "").strip()[:120]

    async def _evaluate_faithfulness(self, case: EvalCase, generated: str) -> float | None:
        """忠实度判分；返回 0~1，判分失败返回 None（不再默认 0.5）。"""
        if self.llm is None:
            self.judge_skipped += 1
            return None
        if not case.reference_answer or not generated:
            self.judge_skipped += 1
            return None
        score, _reason = await self._judge(
            FAITHFULNESS_PROMPT.format(reference=case.reference_answer, answer=generated)
        )
        if score is None:
            self.judge_failures += 1
        return score

    async def _evaluate_relevancy(self, case: EvalCase, generated: str) -> float | None:
        """相关性判分（回答是否切题），口径与 faithfulness 同为 JudgeScore。"""
        if self.llm is None:
            self.judge_skipped += 1
            return None
        if not generated:
            self.judge_skipped += 1
            return None
        score, _reason = await self._judge(
            RELEVANCY_PROMPT.format(query=case.query, answer=generated)
        )
        if score is None:
            self.judge_failures += 1
        return score

    async def _generate_answer(self, case: EvalCase, docs: list[dict]) -> str:
        """基于检索到的 Top-2 文档生成回答（无 LLM 或无文档时返回空串）。"""
        if self.llm is None or not docs:
            return ""
        context = "\n".join(str(doc.get("content", "")) for doc in docs[:2])
        messages = [
            SystemMessage(content="你是金融客服知识库问答助手，只依据给定资料回答，不编造。"),
            HumanMessage(content=f"资料:\n{context}\n\n问题：{case.query}\n\n只给出答案。"),
        ]
        try:
            response = await self.llm.ainvoke(messages)
        except Exception:  # noqa: BLE001 - 生成失败则该用例不参与 judge 均值
            return ""
        return (extract_response_text(response) or "").strip()

    # ── 逐条评估 ──

    async def _evaluate_retrieval(self, case: EvalCase, top_k: int = DEFAULT_TOP_K) -> dict:
        """检索明细（保留旧方法名，可单独调用）。检索深度至少 5 以保证 @5 指标可比。"""
        docs = self._retrieve(case.query, max(top_k, DEFAULT_TOP_K))
        return {"query": case.query, **self._retrieval_detail(case, docs)}

    async def _evaluate_case(
        self, case: EvalCase, top_k: int, confidence_threshold: float
    ) -> dict:
        docs = self._retrieve(case.query, max(top_k, DEFAULT_TOP_K))
        detail = self._retrieval_detail(case, docs)

        entry: dict[str, Any] = {
            "id": case.id,
            "query": case.query,
            "tags": list(case.tags),
            "expected_source": case.expected_source,
            "expected_keywords": list(case.expected_keywords),
            "grounded": case.is_grounded,
            "out_of_scope": case.is_out_of_scope,
            "conversational": case.is_conversational,
            "generated_answer": "",
            "faithfulness": None,
            "answer_relevancy": None,
            "judge_status": "pending",
            **detail,
        }

        # coverage：知识库没有的问题，是否被正确判为"未命中 → 转人工"
        if case.is_out_of_scope:
            entry["coverage_expected"] = True
            entry["coverage_correct"] = bool(detail["best_score"] < confidence_threshold)

        if self.llm is None:
            entry["judge_status"] = "skipped_no_llm"
            if case.reference_answer:
                self.judge_skipped += 1
            return entry

        generated = await self._generate_answer(case, docs)
        entry["generated_answer"] = generated[:300]
        if case.reference_answer and generated:
            entry["faithfulness"] = await self._evaluate_faithfulness(case, generated)
            entry["answer_relevancy"] = await self._evaluate_relevancy(case, generated)
            entry["judge_status"] = "judged"
        elif not generated:
            entry["judge_status"] = "skipped_no_context"
        else:
            entry["judge_status"] = "skipped_no_reference"
        return entry

    # ── 汇总 ──

    async def evaluate(
        self,
        golden_set: GoldenSet | None = None,
        top_k: int = DEFAULT_TOP_K,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> dict:
        """
        运行完整评估。返回::

            {"total_cases": int,
             "metrics": {"recall@3", "recall@5", "mrr", "ndcg@5",
                         "faithfulness", "answer_relevancy", "coverage"},  # round(4)
             "judge_failures": int,
             "cases": [逐条明细],
             "grounded_cases" / "out_of_scope_cases" / "conversational_cases": int,
             "judge_skipped" / "judged_cases": int,
             "dataset": str, "top_k": int, "confidence_threshold": float}

        top_k=None 表示沿用构造时的 self.top_k；无 llm 时 judge 类指标为 None；
        分母为空的指标同样为 None（用 0 会被误读成"效果差"而不是"没测到"）。
        """
        if top_k is None:
            top_k = self.top_k
        golden_set = golden_set if golden_set is not None else GoldenSet.default()
        cases = list(golden_set.cases)
        if not cases:
            return {
                "total_cases": 0,
                "metrics": {
                    key: None
                    for key in (
                        "recall@3", "recall@5", "mrr", "ndcg@5",
                        "faithfulness", "answer_relevancy", "coverage",
                    )
                },
                "judge_failures": 0,
                "judge_skipped": 0,
                "cases": [],
                "dataset": getattr(golden_set, "source", ""),
                "error": "Golden Set 为空",
            }

        self.judge_failures = 0
        self.judge_skipped = 0

        entries = [await self._evaluate_case(case, top_k, confidence_threshold) for case in cases]

        grounded = [e for e in entries if e["grounded"]]
        oos = [e for e in entries if e["out_of_scope"]]
        conversational = [e for e in entries if e["conversational"]]

        faithfulness = mean([e["faithfulness"] for e in entries if e["faithfulness"] is not None])
        relevancy = mean([e["answer_relevancy"] for e in entries if e["answer_relevancy"] is not None])
        coverage_values = [1.0 if e["coverage_correct"] else 0.0 for e in oos]

        def _grounded_mean(key: str) -> float | None:
            return mean([float(e[key]) for e in grounded]) if grounded else None

        metrics = {
            "recall@3": _round(_grounded_mean("recall@3")),
            "recall@5": _round(_grounded_mean("recall@5")),
            "mrr": _round(_grounded_mean("mrr")),
            "ndcg@5": _round(_grounded_mean("ndcg@5")),
            "faithfulness": _round(faithfulness),
            "answer_relevancy": _round(relevancy),
            "coverage": _round(mean(coverage_values)) if coverage_values else None,
        }

        return {
            "total_cases": len(entries),
            "metrics": metrics,
            "judge_failures": self.judge_failures,
            "judge_skipped": self.judge_skipped,
            "cases": entries,
            "grounded_cases": len(grounded),
            "out_of_scope_cases": len(oos),
            "conversational_cases": len(conversational),
            "judged_cases": sum(1 for e in entries if e["faithfulness"] is not None),
            "dataset": getattr(golden_set, "source", ""),
            "top_k": top_k,
            "confidence_threshold": confidence_threshold,
        }


# ── CLI 入口：python -m evaluation.evaluator ─────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.evaluator",
        description="HeteroAgent RAG 离线评估（Golden Set v1 JSONL）",
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET_PATH),
        help="评估数据集路径（JSONL），默认 evaluation/datasets/v1.jsonl",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="检索深度")
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        help="coverage 判定阈值：检索最高分低于它即算未命中并转人工（默认 0.3）",
    )
    parser.add_argument("--model", default="", help="判分模型名，默认取环境变量 MODEL_NAME")
    parser.add_argument(
        "--index-path",
        default="",
        help="向量库路径，默认取环境变量 FAISS_INDEX_PATH（再默认 ./vector_store/faiss_index）",
    )
    parser.add_argument(
        "--memory",
        choices=("auto", "faiss", "stub"),
        default="auto",
        help="记忆后端：auto 优先真实 LongTermMemory，失败退回离线替身",
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="离线模式：不调用 LLM，judge 类指标为 None"
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON 输出缩进")
    return parser


def _resolve_llm(args: argparse.Namespace):
    """无 --no-llm 且有 API key 时才构造判分模型；否则返回 None（评估仍可跑检索指标）。"""
    if args.no_llm:
        return None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[evaluator] 未配置 OPENAI_API_KEY，跳过 LLM 判分（judge 指标为 None）", file=sys.stderr)
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - 环境缺包时降级
        print(f"[evaluator] langchain-openai 不可用，跳过 LLM 判分: {exc}", file=sys.stderr)
        return None
    return ChatOpenAI(model=args.model or os.getenv("MODEL_NAME", "gpt-4o"), temperature=0)


def _resolve_memory(args: argparse.Namespace) -> tuple[Any, str]:
    index_path = args.index_path or os.getenv("FAISS_INDEX_PATH", "") or "./vector_store/faiss_index"
    if args.memory in ("auto", "faiss"):
        try:
            from memory.long_term import LongTermMemory

            memory = LongTermMemory(index_path=index_path)
            load_corpus(memory)
            return memory, f"LongTermMemory({index_path})"
        except Exception as exc:  # noqa: BLE001
            if args.memory == "faiss":
                raise
            print(f"[evaluator] LongTermMemory 不可用，改用离线检索替身: {exc}", file=sys.stderr)
    return StubKnowledgeMemory(), "StubKnowledgeMemory"


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        golden_set = GoldenSet.load(args.dataset)
    except FileNotFoundError:
        print(f"[evaluator] 数据集不存在: {args.dataset}，回退内置用例", file=sys.stderr)
        golden_set = GoldenSet.default()

    memory, memory_backend = _resolve_memory(args)
    llm = _resolve_llm(args)

    evaluator = Evaluator(llm=llm, long_term_memory=memory, top_k=args.top_k)
    report = asyncio.run(
        evaluator.evaluate(
            golden_set,
            top_k=args.top_k,
            confidence_threshold=args.confidence_threshold,
        )
    )
    report["runtime"] = {
        "memory_backend": memory_backend,
        "llm": "on" if llm else "off",
        "dataset": golden_set.source,
    }
    print(json.dumps(report, ensure_ascii=False, indent=args.indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

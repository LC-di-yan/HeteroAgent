"""evaluation.evaluator 离线单测（T-P2-04 / P0-09）。

全部离线：检索用 FakeMemory 桩（固定 search 返回可控文档列表），
判分用 conftest 的 fake_llm（FakeLLM：ainvoke 按队列返回文本、with_structured_output 返回 self）。
手算对表：期望文档排第 2 位时 MRR = 0.5，nDCG@5 = 1/log2(3) 约等于 0.6309。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest
from pydantic import ValidationError

import evaluation.evaluator as ev_mod
from evaluation.evaluator import (
    DEFAULT_DATASET_PATH,
    KNOWLEDGE_BASE_CORPUS,
    EvalCase,
    Evaluator,
    GoldenSet,
    JudgeScore,
    StubKnowledgeMemory,
    dcg_at_k,
    ndcg_at_k,
    parse_judge_score,
    strip_code_fences,
)

DATASET_PATH = DEFAULT_DATASET_PATH

NL = chr(10)          # 换行（避免测试源码里出现转义序列）
TICKS = chr(96) * 3   # 代码围栏三反引号
BOM = bytes([0xEF, 0xBB, 0xBF])  # UTF-8 BOM


# ── 桩 ────────────────────────────────────────────────────────────────


class FakeMemory:
    """只实现评估所需的同步 search(query, top_k)，返回可控文档列表。"""

    def __init__(self, docs=None, mapping=None):
        self.docs = list(docs or [])
        self.mapping = dict(mapping or {})
        self.calls = []

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        self.calls.append({"query": query, "top_k": top_k})
        docs = self.mapping.get(query, self.docs)
        return [dict(d) for d in docs[: max(0, top_k)]]


class _Msg:
    """最小消息对象（与 conftest.FakeLLM 的返回结构一致）。"""

    def __init__(self, content: str) -> None:
        self.content = content


class StructuredJudgeLLM:
    """with_structured_output 走独立 Runnable（返回 JudgeScore）；plain ainvoke 仅用于生成。"""

    def __init__(self) -> None:
        self.structured_calls = 0
        self.plain_calls = 0

    def with_structured_output(self, schema, **kwargs):
        self.structured_calls += 1
        assert schema is JudgeScore

        class _Runnable:

            async def ainvoke(self, messages, **kw):
                return JudgeScore(score=0.92, reason="完全依据参考信息")

        return _Runnable()

    async def ainvoke(self, messages, **kwargs):
        self.plain_calls += 1
        return _Msg("理财产品A年化收益率为3.5%-5.2%。")


class LegacyLLM:
    """没有 with_structured_output 的旧模型桩，触发 AttributeError 回退 + 文本解析。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return _Msg("分数: 0.4")


# ── 工具 ──────────────────────────────────────────────────────────────


def make_doc(source: str, content: str, score: float) -> dict:
    return {"id": f"doc-{source}-{score}", "source": source, "content": content, "score": score}


def run_eval(memory, cases, llm=None, **kwargs) -> dict:
    ev = Evaluator(llm=llm, long_term_memory=memory, top_k=kwargs.pop("instance_top_k", 5))
    return asyncio.run(ev.evaluate(GoldenSet(cases=cases), **kwargs))


REFUND_CONTENT = KNOWLEDGE_BASE_CORPUS["refund_policy.md"]
PRODUCT_CONTENT = KNOWLEDGE_BASE_CORPUS["product_faq.md"]
ACCOUNT_CONTENT = KNOWLEDGE_BASE_CORPUS["account_guide.md"]


# ── 1) 检索指标：手算对表 ─────────────────────────────────────────────


def test_recall_mrr_when_expected_doc_at_rank_two():
    docs = [
        make_doc("product_faq.md", PRODUCT_CONTENT, 0.90),
        make_doc("refund_policy.md", REFUND_CONTENT, 0.80),
        make_doc("account_guide.md", ACCOUNT_CONTENT, 0.10),
    ]
    case = EvalCase(
        query="退款多久到账？",
        expected_content="退款政策",
        reference_answer="3-5个工作日原路退回",
        id="t-001",
        expected_source="refund_policy.md",
        expected_keywords=["原路退回"],
    )
    report = run_eval(FakeMemory(docs=docs), [case], top_k=5, confidence_threshold=0.3)

    assert report["total_cases"] == 1
    m = report["metrics"]
    assert m["recall@3"] == 1.0
    assert m["recall@5"] == 1.0
    assert m["mrr"] == 0.5  # 期望文档排第 2 位，倒数排名 1/2
    assert m["ndcg@5"] == pytest.approx(0.6309, abs=1e-4)  # 1/log2(3)
    entry = report["cases"][0]
    assert entry["hit_ranks"] == [2]
    assert entry["best_score"] == 0.9


def test_ndcg_two_relevant_docs_ranks_one_and_three():
    docs = [
        make_doc("product_faq.md", PRODUCT_CONTENT + "（分块1）", 0.9),
        make_doc("refund_policy.md", REFUND_CONTENT, 0.5),
        make_doc("product_faq.md", PRODUCT_CONTENT + "（分块2）", 0.4),
    ]
    case = EvalCase(
        query="理财产品A收益如何？",
        expected_content="理财产品A",
        id="t-002",
        expected_source="product_faq.md",
        expected_keywords=["年化收益率"],
    )
    report = run_eval(FakeMemory(docs=docs), [case])
    # DCG = 1/log2(2) + 1/log2(4) = 1.5；IDCG = 1/log2(2) + 1/log2(3) 约 1.6309
    assert report["metrics"]["ndcg@5"] == pytest.approx(0.9197, abs=1e-4)
    assert report["metrics"]["mrr"] == 1.0
    assert report["metrics"]["recall@3"] == 1.0


def test_no_relevant_doc_gives_zero_metrics():
    docs = [
        make_doc("product_faq.md", PRODUCT_CONTENT, 0.7),
        make_doc("account_guide.md", ACCOUNT_CONTENT, 0.5),
    ]
    case = EvalCase(
        query="退款多久到账？",
        expected_content="退款政策",
        id="t-003",
        expected_source="refund_policy.md",
        expected_keywords=["原路退回"],
    )
    report = run_eval(FakeMemory(docs=docs), [case])
    m = report["metrics"]
    assert m["recall@3"] == 0.0
    assert m["recall@5"] == 0.0
    assert m["mrr"] == 0.0
    assert m["ndcg@5"] == 0.0


def test_keyword_hit_in_content_when_source_differs():
    docs = [
        make_doc("conversation_general", "欢迎咨询，这里是智能客服。", 0.6),
        make_doc("misc_notes.md", "客服手册摘录：退款将在3-5个工作日内原路退回。", 0.55),
    ]
    case = EvalCase(
        query="退款多久到账？",
        expected_content="",
        id="t-004",
        expected_source="refund_policy.md",
        expected_keywords=["原路退回"],
    )
    report = run_eval(FakeMemory(docs=docs), [case])
    # source 不匹配，但 expected_keywords 命中 content，命中排在第 2 位
    assert report["metrics"]["mrr"] == 0.5
    assert report["cases"][0]["hit_ranks"] == [2]


def test_metrics_helpers_pure_functions():
    assert dcg_at_k([1.0, 0.0, 1.0], 5) == pytest.approx(1.5)  # 1/log2(2)+1/log2(4)
    assert ndcg_at_k([1.0, 0.0, 1.0], 3) == pytest.approx(0.9197, abs=1e-4)
    assert ndcg_at_k([0.0, 1.0], 5) == pytest.approx(0.6309, abs=1e-4)
    assert ndcg_at_k([0.0, 0.0, 0.0], 5) == 0.0


def test_conversational_and_oos_excluded_from_retrieval_denominator():
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.85)]
    cases = [
        EvalCase(query="退款多久到账？", expected_content="退款政策", id="g1",
                 expected_source="refund_policy.md", expected_keywords=["原路退回"]),
        EvalCase(query="你好", expected_content="", id="c1",
                 expected_source="conversation_general", tags=["general"]),
        EvalCase(query="黄金价格预测", expected_content="", id="o1",
                 expected_source="out_of_scope", tags=["out_of_scope"]),
    ]
    report = run_eval(FakeMemory(docs=docs), cases, top_k=5, confidence_threshold=0.3)
    assert report["grounded_cases"] == 1
    assert report["conversational_cases"] == 1
    assert report["out_of_scope_cases"] == 1
    assert report["metrics"]["recall@5"] == 1.0  # 分母只含 grounded 用例


# ── 2) coverage：out_of_scope 未命中判定 ──────────────────────────────


def test_coverage_low_score_counts_as_correct_handoff():
    docs = [
        make_doc("product_faq.md", PRODUCT_CONTENT, 0.12),
        make_doc("refund_policy.md", REFUND_CONTENT, 0.25),
    ]
    cases = [
        EvalCase(query="黄金价格走势", expected_content="", id="o1",
                 expected_source="out_of_scope", reference_answer="无相关信息，转人工"),
        EvalCase(query="贷款利率", expected_content="", id="o2",
                 expected_source="out_of_scope"),
    ]
    report = run_eval(FakeMemory(docs=docs), cases, top_k=5, confidence_threshold=0.3)
    assert report["metrics"]["coverage"] == 1.0
    assert all(e["coverage_correct"] for e in report["cases"])


def test_coverage_high_score_counts_as_miss():
    docs = [make_doc("product_faq.md", PRODUCT_CONTENT, 0.8)]
    cases = [
        EvalCase(query="黄金价格走势", expected_content="", id="o1",
                 expected_source="out_of_scope"),
        EvalCase(query="贷款利率", expected_content="", id="o2",
                 expected_source="out_of_scope"),
    ]
    report = run_eval(FakeMemory(docs=docs), cases, top_k=5, confidence_threshold=0.3)
    assert report["metrics"]["coverage"] == 0.0
    # 阈值收紧到 0.9 后，同样的命中分数也被判为未命中并转人工，coverage 变为正确
    report2 = run_eval(FakeMemory(docs=docs), cases, top_k=5, confidence_threshold=0.9)
    assert report2["metrics"]["coverage"] == 1.0


def test_coverage_empty_retrieval_counts_as_correct():
    cases = [EvalCase(query="黄金价格走势", expected_content="", id="o1",
                      expected_source="out_of_scope")]
    report = run_eval(FakeMemory(docs=[]), cases, top_k=5, confidence_threshold=0.3)
    assert report["metrics"]["coverage"] == 1.0
    assert report["cases"][0]["best_score"] == 0.0


def test_coverage_none_without_oos_cases():
    cases = [EvalCase(query="退款多久到账？", expected_content="退款政策", id="g1",
                      expected_source="refund_policy.md", expected_keywords=["原路退回"])]
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9)]
    report = run_eval(FakeMemory(docs=docs), cases)
    assert report["metrics"]["coverage"] is None


# ── 3) LLM-as-Judge：结构化优先 / 文本兜底 / 失败计数 ─────────────────


def test_faithfulness_parses_plain_float(fake_llm):
    fake_llm.responses = ["理财产品A退款将在3-5个工作日内原路退回。", "0.85", "0.9"]
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9)]
    case = EvalCase(query="退款多久到账？", expected_content="退款政策",
                    reference_answer="3-5个工作日内原路退回", id="j1",
                    expected_source="refund_policy.md", expected_keywords=["原路退回"])
    report = run_eval(FakeMemory(docs=docs), [case], llm=fake_llm)
    m = report["metrics"]
    assert m["faithfulness"] == 0.85
    assert m["answer_relevancy"] == 0.9
    assert report["judge_failures"] == 0
    assert report["cases"][0]["judge_status"] == "judged"
    assert report["cases"][0]["generated_answer"]


def test_parse_failure_counts_as_judge_failure_not_neutral_half(fake_llm):
    # FakeLLM 默认队列只有 "knowledge_rag"：生成用掉之后，两问判分都拿不到合法分数
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9)]
    case = EvalCase(query="退款多久到账？", expected_content="退款政策",
                    reference_answer="3-5个工作日内原路退回", id="j2",
                    expected_source="refund_policy.md", expected_keywords=["原路退回"])
    report = run_eval(FakeMemory(docs=docs), [case], llm=fake_llm)
    assert report["judge_failures"] == 2  # faithfulness 与 answer_relevancy 各计一次
    assert report["metrics"]["faithfulness"] is None  # 不再默默给中性 0.5
    assert report["metrics"]["answer_relevancy"] is None
    assert report["judged_cases"] == 0


def test_structured_output_path_preferred():
    llm = StructuredJudgeLLM()
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9)]
    case = EvalCase(query="退款多久到账？", expected_content="退款政策",
                    reference_answer="3-5个工作日内原路退回", id="j3",
                    expected_source="refund_policy.md", expected_keywords=["原路退回"])
    report = run_eval(FakeMemory(docs=docs), [case], llm=llm)
    assert llm.structured_calls == 2  # faithfulness + answer_relevancy
    assert llm.plain_calls == 1       # 只有答案生成走 plain ainvoke
    assert report["metrics"]["faithfulness"] == 0.92
    assert report["metrics"]["answer_relevancy"] == 0.92


def test_attribute_error_falls_back_to_text_parsing():
    llm = LegacyLLM()  # 无 with_structured_output
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9)]
    case = EvalCase(query="退款多久到账？", expected_content="退款政策",
                    reference_answer="3-5个工作日内原路退回", id="j4",
                    expected_source="refund_policy.md", expected_keywords=["原路退回"])
    report = run_eval(FakeMemory(docs=docs), [case], llm=llm)
    assert llm.calls == 3  # 生成 + faithfulness + answer_relevancy
    assert report["metrics"]["faithfulness"] == 0.4
    assert report["judge_failures"] == 0


def test_judge_score_schema_bounds():
    assert JudgeScore(score=0.5, reason="ok").score == 0.5
    with pytest.raises(ValidationError):
        JudgeScore(score=1.5, reason="越界")
    with pytest.raises(ValidationError):
        JudgeScore(score=-0.1, reason="越界")


# ── 4) 文本解析兜底：剥围栏 + 首个 [0,1] 浮点 ─────────────────────────


def test_parse_judge_score_variants():
    assert parse_judge_score("0.85") == 0.85
    assert parse_judge_score("评分：0.85（满分1.0）") == 0.85
    assert parse_judge_score('json {"score": 0.8, "reason": "忠实"}') == 0.8
    assert parse_judge_score("1.0") == 1.0
    assert parse_judge_score("完全无法判断") is None
    assert parse_judge_score("评分 7/10 分") is None  # 无 0~1 浮点，判失败而不是取整
    assert parse_judge_score("-0.2") is None
    assert parse_judge_score("") is None
    assert parse_judge_score(None) is None


def test_strip_code_fences_keeps_inner_text():
    fenced = NL.join([TICKS + "json", '{"score": 0.8, "reason": "完全忠实"}', TICKS])
    stripped = strip_code_fences(fenced)
    assert "score" in stripped
    assert TICKS not in stripped
    assert strip_code_fences("0.9") == "0.9"


def test_parse_judge_score_reads_fenced_json():
    fenced = NL.join([TICKS + "json", '{"score": 0.8, "reason": "完全忠实"}', TICKS])
    assert parse_judge_score(fenced) == 0.8


def test_parse_failure_prefers_first_in_range_float():
    # "2.5" 越界跳过，取首个落在 [0,1] 的浮点
    assert parse_judge_score("得分 2.5，归一化后 0.5") == 0.5


# ── 5) 数据集：字段齐全 / 条数 / 合法 JSONL ───────────────────────────


def test_dataset_load_has_enough_complete_cases():
    gs = GoldenSet.load(DATASET_PATH)
    assert len(gs.cases) >= 30
    ids = []
    oos = 0
    for case in gs.cases:
        assert case.id and case.query
        assert case.expected_source in ev_mod.ALLOWED_SOURCES
        assert isinstance(case.expected_keywords, list)
        assert isinstance(case.tags, list) and case.tags
        assert isinstance(case.reference_answer, str) and case.reference_answer
        ids.append(case.id)
        if case.expected_source == "out_of_scope":
            oos += 1
            assert case.expected_keywords == []
        if case.is_grounded:
            corpus = KNOWLEDGE_BASE_CORPUS[case.expected_source]
            assert case.expected_keywords
            for kw in case.expected_keywords:
                assert kw in corpus, f"{case.id} 关键词不在语料: {kw}"
    assert len(set(ids)) == len(ids), "用例 id 必须唯一"
    assert oos == 5, "out_of_scope 用例必须正好 5 条"


def test_dataset_raw_jsonl_is_legal():
    raw = DATASET_PATH.read_bytes()
    assert not raw.startswith(BOM), "必须 UTF-8 无 BOM"
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 30
    required = {"id", "query", "expected_source", "expected_keywords", "reference_answer", "tags"}
    for line in lines:
        obj = json.loads(line)  # 每行都是独立合法 JSON
        assert required <= set(obj), f"缺字段: {required - set(obj)}"
    for obj in (json.loads(line) for line in lines[:3]):
        assert set(obj) == required


def test_dataset_tag_distribution():
    gs = GoldenSet.load(DATASET_PATH)
    counter = Counter(tag for case in gs.cases for tag in case.tags)
    assert counter["out_of_scope"] == 5
    assert counter["refund"] == 8
    assert counter["account"] == 9
    assert counter["complaint"] >= 1
    assert counter["human_handoff"] >= 1
    for case in gs.cases:
        assert case.tags, f"{case.id} 缺少 tags"


def test_dataset_sources_cover_all_categories():
    gs = GoldenSet.load(DATASET_PATH)
    sources = {case.expected_source for case in gs.cases}
    assert sources == set(ev_mod.ALLOWED_SOURCES)


# ── 6) GoldenSet.default() 回退 / load 容错 ───────────────────────────


def test_default_reads_versioned_dataset():
    gs = GoldenSet.default()
    assert len(gs.cases) >= 30
    assert gs.source.endswith("v1.jsonl")


def test_default_falls_back_to_builtin_four_when_dataset_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ev_mod, "DEFAULT_DATASET_PATH", tmp_path / "missing.jsonl")
    gs = GoldenSet.default()
    assert len(gs.cases) == 4
    assert gs.source == "builtin"
    queries = {case.query for case in gs.cases}
    assert "理财产品A的收益率是多少？" in queries
    assert "我买了产品想退款可以吗？" in queries


def test_load_raises_on_invalid_json_line(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(NL.join(['{"id":"x","query":"q"}', "not-json", ""]), encoding="utf-8")
    with pytest.raises(ValueError, match="合法 JSON"):
        GoldenSet.load(bad)


def test_load_raises_on_missing_query(tmp_path):
    bad = tmp_path / "no_query.jsonl"
    bad.write_text('{"id":"x","expected_source":"product_faq.md"}' + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="query"):
        GoldenSet.load(bad)


def test_load_raises_on_bom(tmp_path):
    bad = tmp_path / "bom.jsonl"
    bad.write_bytes(BOM + b'{"id":"x","query":"q"}')
    with pytest.raises(ValueError, match="BOM"):
        GoldenSet.load(bad)


def test_load_skips_blank_lines_and_supports_legacy_field(tmp_path):
    file = tmp_path / "legacy.jsonl"
    file.write_text(
        NL.join([
            '{"id":"a","query":"q1","expected_content":"退款政策","reference_answer":"r"}',
            "",
            '{"id":"b","query":"q2","expected_source":"out_of_scope"}',
            "",
        ]),
        encoding="utf-8",
    )
    gs = GoldenSet.load(file)
    assert [c.id for c in gs.cases] == ["a", "b"]
    assert gs.cases[0].is_grounded  # 旧字段 expected_content 也算有金标签
    assert gs.cases[0].expected_content == "退款政策"
    assert gs.cases[1].is_out_of_scope


def test_load_rejects_unknown_source(tmp_path):
    file = tmp_path / "bad_source.jsonl"
    file.write_text('{"id":"x","query":"q","expected_source":"nope.md"}' + NL, encoding="utf-8")
    with pytest.raises(ValueError, match="expected_source"):
        GoldenSet.load(file)


# ── 7) 报告结构与记忆接口约束 ─────────────────────────────────────────


def test_report_shape_and_rounding():
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9),
            make_doc("product_faq.md", PRODUCT_CONTENT, 0.4)]
    cases = [
        EvalCase(query="退款多久到账？", expected_content="退款政策", id="g1",
                 expected_source="refund_policy.md", expected_keywords=["原路退回"]),
        EvalCase(query="黄金价格预测", expected_content="", id="o1",
                 expected_source="out_of_scope", reference_answer="转人工"),
    ]
    report = run_eval(FakeMemory(docs=docs), cases, top_k=5, confidence_threshold=0.3)
    for key in ("total_cases", "metrics", "judge_failures", "cases"):
        assert key in report
    assert report["total_cases"] == 2
    for key in ("recall@3", "recall@5", "mrr", "ndcg@5", "faithfulness",
                "answer_relevancy", "coverage"):
        assert key in report["metrics"]
    for value in report["metrics"].values():
        assert value is None or isinstance(value, float)
        assert value is None or round(value, 4) == value
    entry = report["cases"][0]
    for key in ("query", "recall@3", "recall@5", "mrr", "top1"):
        assert key in entry  # 旧字段保留，向后兼容
    assert "expected_source" in entry and "hit_ranks" in entry


def test_memory_interface_is_only_sync_search():
    docs = [make_doc("refund_policy.md", REFUND_CONTENT, 0.9),
            make_doc("product_faq.md", PRODUCT_CONTENT, 0.4)]
    cases = [
        EvalCase(query="退款多久到账？", expected_content="退款政策", id="g1",
                 expected_source="refund_policy.md", expected_keywords=["原路退回"]),
        EvalCase(query="黄金价格预测", expected_content="", id="o1",
                 expected_source="out_of_scope"),
    ]
    memory = FakeMemory(docs=docs)
    run_eval(memory, cases, top_k=5)
    assert memory.calls
    for call in memory.calls:
        assert set(call) == {"query", "top_k"}
        assert call["top_k"] == max(5, 5)  # 检索深度取 max(top_k, 5)


def test_evaluate_without_memory_raises_clear_error():
    ev = Evaluator(llm=None, long_term_memory=None)
    with pytest.raises(ValueError, match="long_term_memory"):
        asyncio.run(ev.evaluate(GoldenSet(cases=[EvalCase(query="q", expected_content="k")])))


def test_evaluate_empty_golden_set_returns_empty_report():
    ev = Evaluator(llm=None, long_term_memory=FakeMemory())
    report = asyncio.run(ev.evaluate(GoldenSet(cases=[])))
    assert report["total_cases"] == 0
    assert report["cases"] == []
    assert report["metrics"]["recall@5"] is None


def test_legacy_positional_evalcase_still_supported():
    case = EvalCase("理财产品A的收益率是多少？", "理财产品A", "年化收益率3.5%-5.2%")
    assert case.query.startswith("理财产品")
    assert case.expected_content == "理财产品A"
    assert case.is_grounded


def test_stub_memory_is_deterministic_and_offline_safe():
    memory = StubKnowledgeMemory()
    hits = memory.search("退款多久到账？", top_k=5)
    assert hits, "退款问题应至少命中退款文档"
    assert hits[0]["source"] == "refund_policy.md"
    assert 0.0 <= hits[0]["score"] <= 1.0
    again = memory.search("退款多久到账？", top_k=5)
    assert [h["id"] for h in again] == [h["id"] for h in hits]


def test_load_corpus_fills_stub_memory():
    memory = StubKnowledgeMemory(corpus={})
    assert ev_mod.load_corpus(memory) == 3
    assert len(memory._docs) == 3


# ── 8) CLI 入口 ───────────────────────────────────────────────────────


def test_cli_arg_parser_defaults():
    parser = ev_mod.build_arg_parser()
    args = parser.parse_args([])
    assert args.dataset.endswith("v1.jsonl")
    assert args.top_k == 5
    assert args.confidence_threshold == 0.3
    assert args.no_llm is False
    args2 = parser.parse_args([
        "--dataset", "x.jsonl",
        "--top-k", "3",
        "--confidence-threshold", "0.1",
        "--no-llm",
    ])
    assert (args2.dataset, args2.top_k, args2.confidence_threshold, args2.no_llm) == (
        "x.jsonl", 3, 0.1, True)


def test_cli_main_runs_offline_with_stub_memory(capsys):
    exit_code = ev_mod.main([
        "--dataset", str(DATASET_PATH),
        "--no-llm",
        "--memory", "stub",
        "--top-k", "5",
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["total_cases"] >= 30
    assert report["metrics"]["faithfulness"] is None
    assert report["metrics"]["answer_relevancy"] is None
    assert report["judge_failures"] == 0
    assert report["runtime"]["memory_backend"] == "StubKnowledgeMemory"
    assert report["runtime"]["llm"] == "off"
    assert report["dataset"].endswith("v1.jsonl")


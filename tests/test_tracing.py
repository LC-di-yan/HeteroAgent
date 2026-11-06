"""tracing/otel_config 的 GenAI 语义约定测试（T-P1-08，全离线）。

用 InMemorySpanExporter + SimpleSpanProcessor 直接验收 span 命名、gen_ai.* 属性、
父子层级、token/成本记账、装饰器双路径、采样映射与指标分位——不依赖任何网络或后端。
"""
from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import StatusCode

import tracing.otel_config as otel
from tracing.otel_config import (
    GEN_AI_AGENT_NAME,
    GEN_AI_COST_USD,
    GEN_AI_FINISH_REASONS,
    GEN_AI_INPUT_TOKENS,
    GEN_AI_OPERATION,
    GEN_AI_OUTPUT_TOKENS,
    GEN_AI_PROVIDER,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_TOOL_NAME,
    SESSION_ID,
    AgentMetrics,
    agent_span,
    build_sampler,
    estimate_cost,
    get_cost_summary,
    inference_span,
    record_usage,
    reset_cost_tracker,
    retrieval_span,
    set_global_metrics,
    tool_span,
    trace_agent_call,
)

# ══════════════════════════════════════════════════════════════════════
# 测试基座：临时把模块 tracer 换成 InMemory provider，退出时还原
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def memory_tracer():
    """安装 InMemorySpanExporter provider，测试结束还原模块状态，互不污染。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=build_sampler("always_on"))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous_provider = otel.install_provider(provider)
    previous_metrics = otel.get_global_metrics()
    set_global_metrics(None)
    reset_cost_tracker()
    yield exporter
    reset_cost_tracker()
    set_global_metrics(previous_metrics)
    if previous_provider is None:
        otel.reset_tracing()
    else:
        otel.install_provider(previous_provider)


# ══════════════════════════════════════════════════════════════════════
# 1. 分层 span 工厂：命名 + gen_ai.* 属性 + 父子层级
# ══════════════════════════════════════════════════════════════════════


def test_span_factories_names_and_genai_attributes(memory_tracer):
    with agent_span("knowledge_rag", session_id="sess-1"):
        with inference_span(
            "gpt-5.1", attributes={GEN_AI_RESPONSE_MODEL: "gpt-5.1-2026"}
        ) as inf:
            record_usage(inf, input_tokens=1200, output_tokens=300, finish_reasons=["stop"])
        with tool_span("query_order", tool_call_id="call_1"):
            pass
        with retrieval_span("faiss", top_k=5):
            pass
    with retrieval_span(operation="embeddings", model="text-embedding-3-small"):
        pass

    spans = {s.name: s for s in memory_tracer.get_finished_spans()}
    assert set(spans) == {
        "invoke_agent knowledge_rag",
        "chat gpt-5.1",
        "execute_tool query_order",
        "retrieval faiss",
        "embeddings",
    }

    # ── agent span ──
    a = spans["invoke_agent knowledge_rag"].attributes
    assert a[GEN_AI_OPERATION] == "invoke_agent"
    assert a[GEN_AI_AGENT_NAME] == "knowledge_rag"
    assert a["agent.name"] == "knowledge_rag"          # 旧键双写一个版本周期
    assert a[SESSION_ID] == "sess-1"

    # ── inference span ──
    i = spans["chat gpt-5.1"].attributes
    assert i[GEN_AI_OPERATION] == "chat"
    assert i[GEN_AI_REQUEST_MODEL] == "gpt-5.1"
    assert i[GEN_AI_PROVIDER] == "openai"
    assert i[GEN_AI_RESPONSE_MODEL] == "gpt-5.1-2026"
    assert i[GEN_AI_INPUT_TOKENS] == 1200
    assert i[GEN_AI_OUTPUT_TOKENS] == 300
    assert list(i[GEN_AI_FINISH_REASONS]) == ["stop"]
    assert i["duration_ms"] >= 0

    # ── tool span ──
    t = spans["execute_tool query_order"].attributes
    assert t[GEN_AI_OPERATION] == "execute_tool"
    assert t[GEN_AI_TOOL_NAME] == "query_order"
    assert t["gen_ai.tool.call.id"] == "call_1"

    # ── retrieval / embeddings span ──
    r = spans["retrieval faiss"].attributes
    assert r[GEN_AI_OPERATION] == "retrieval"
    assert r["retrieval.store"] == "faiss"
    assert r["retrieval.top_k"] == 5
    e = spans["embeddings"].attributes
    assert e[GEN_AI_OPERATION] == "embeddings"
    assert e[GEN_AI_REQUEST_MODEL] == "text-embedding-3-small"

    # ── 层级：inference / tool / retrieval 挂在 agent span 下 ──
    parent = spans["invoke_agent knowledge_rag"]
    for child in ("chat gpt-5.1", "execute_tool query_order", "retrieval faiss"):
        assert spans[child].parent.span_id == parent.context.span_id


# ══════════════════════════════════════════════════════════════════════
# 2. record_usage：token/成本写入 + get_cost_summary 累加 + 边界
# ══════════════════════════════════════════════════════════════════════


def test_record_usage_writes_tokens_and_cost(memory_tracer):
    with inference_span("gpt-5.1") as span:
        info = record_usage(span, input_tokens=1000, output_tokens=500)

    assert info["model"] == "gpt-5.1"
    assert info["priced"] is True
    expected = estimate_cost("gpt-5.1", 1000, 500)  # 0.00125 + 0.005 = 0.00625
    assert expected == pytest.approx(0.00625)
    assert info["cost_usd"] == pytest.approx(expected)
    assert info["total_tokens"] == 1500

    attrs = memory_tracer.get_finished_spans()[0].attributes
    assert attrs[GEN_AI_INPUT_TOKENS] == 1000
    assert attrs[GEN_AI_OUTPUT_TOKENS] == 500
    assert attrs[GEN_AI_COST_USD] == pytest.approx(0.00625)
    assert attrs["gen_ai.usage.currency"] == "USD"


def test_cost_summary_accumulates_across_two_calls(memory_tracer):
    for _ in range(2):
        with inference_span("gpt-4o") as span:
            record_usage(span, input_tokens=2000, output_tokens=1000)

    entry = get_cost_summary()["gpt-4o"]
    assert entry["calls"] == 2
    assert entry["input_tokens"] == 4000
    assert entry["output_tokens"] == 2000
    assert entry["total_tokens"] == 6000
    assert entry["cost_usd"] == pytest.approx(2 * (2000 / 1000 * 0.0025 + 1000 / 1000 * 0.01))


def test_record_usage_unknown_model_keeps_tokens_zero_cost(memory_tracer):
    with inference_span("mystery-9") as span:
        info = record_usage(span, input_tokens=700, output_tokens=100)

    assert info["priced"] is False
    assert info["cost_usd"] == 0.0
    entry = get_cost_summary()["mystery-9"]
    assert entry["input_tokens"] == 700
    assert entry["output_tokens"] == 100
    assert entry["cost_usd"] == 0.0


def test_cost_tracker_is_thread_safe(memory_tracer):
    calls_per_worker = 25

    def worker() -> None:
        for _ in range(calls_per_worker):
            record_usage(None, input_tokens=1000, output_tokens=500, model="gpt-4o")

    workers = 8
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in range(workers):
            pool.submit(worker)

    entry = get_cost_summary()["gpt-4o"]
    total = workers * calls_per_worker
    assert entry["calls"] == total
    assert entry["input_tokens"] == total * 1000
    assert entry["output_tokens"] == total * 500
    assert entry["cost_usd"] == pytest.approx(total * estimate_cost("gpt-4o", 1000, 500))


# ══════════════════════════════════════════════════════════════════════
# 3. trace_agent_call：成功 / 异常双路径 + 旧属性双写
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_trace_agent_call_success_and_error_paths(memory_tracer):
    metrics = AgentMetrics()
    set_global_metrics(metrics)

    class RagAgent:
        @trace_agent_call("knowledge_rag")
        async def generate(self, state):
            return {"answer": "ok"}

        @trace_agent_call("knowledge_rag")
        async def boom(self, state):
            raise ValueError("模型超时")

    agent = RagAgent()
    assert await agent.generate({"session_id": "sess-9"}) == {"answer": "ok"}
    with pytest.raises(ValueError):
        await agent.boom({"session_id": "sess-9"})

    spans = {s.name: s for s in memory_tracer.get_finished_spans()}
    assert set(spans) == {
        "invoke_agent knowledge_rag.generate",
        "invoke_agent knowledge_rag.boom",
    }

    ok = spans["invoke_agent knowledge_rag.generate"]
    assert ok.attributes[GEN_AI_OPERATION] == "invoke_agent"
    assert ok.attributes[GEN_AI_AGENT_NAME] == "knowledge_rag"
    assert ok.attributes["agent.name"] == "knowledge_rag"
    assert ok.attributes["agent.method"] == "generate"
    assert ok.attributes["agent.success"] is True
    assert ok.attributes["agent.duration_ms"] >= 0.0
    assert ok.attributes[SESSION_ID] == "sess-9"
    assert ok.status.status_code is not StatusCode.ERROR

    bad = spans["invoke_agent knowledge_rag.boom"]
    assert bad.status.status_code is StatusCode.ERROR
    assert bad.attributes["agent.success"] is False
    assert bad.attributes["agent.error_type"] == "ValueError"
    assert bad.attributes["agent.duration_ms"] >= 0.0
    assert "exception" in [event.name for event in bad.events]

    summary = metrics.get_summary()["knowledge_rag"]
    assert summary["total_calls"] == 2
    assert summary["error_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_trace_agent_call_records_metrics_even_without_tracer(memory_tracer, monkeypatch):
    """OTel 缺席（get_tracer 返回 None）时指标口径不变——api 无 OTel 也能跑。"""
    monkeypatch.setattr(otel, "get_tracer", lambda name=None: None)
    metrics = AgentMetrics()
    set_global_metrics(metrics)

    class SimpleAgent:
        @trace_agent_call("supervisor")
        async def route(self):
            return "ok"

    assert await SimpleAgent().route() == "ok"
    summary = metrics.get_summary()["supervisor"]
    assert summary["total_calls"] == 1
    assert summary["error_rate"] == 0.0


# ══════════════════════════════════════════════════════════════════════
# 4. build_sampler：6 种官方映射 + 未知名回退
# ══════════════════════════════════════════════════════════════════════


def test_build_sampler_maps_all_six_official_names():
    assert build_sampler("always_on").get_description() == "AlwaysOnSampler"
    assert build_sampler("always_off").get_description() == "AlwaysOffSampler"

    on = build_sampler("parentbased_always_on")
    assert isinstance(on, ParentBased)
    assert "root:AlwaysOnSampler" in on.get_description()

    off = build_sampler("parentbased_always_off")
    assert isinstance(off, ParentBased)
    assert "root:AlwaysOffSampler" in off.get_description()

    ratio = build_sampler("traceidratio", 0.25)
    assert isinstance(ratio, TraceIdRatioBased)
    assert ratio.rate == pytest.approx(0.25)

    parent_ratio = build_sampler("parentbased_traceidratio", 0.4)
    assert isinstance(parent_ratio, ParentBased)
    assert "root:TraceIdRatioBased{0.4}" in parent_ratio.get_description()


def test_build_sampler_unknown_name_falls_back_to_parentbased_always_on():
    for name in ("", "nonsense", "PARENT_BASED_ALWAYS_ON "):
        fallback = build_sampler(name)
        assert isinstance(fallback, ParentBased)
        assert "root:AlwaysOnSampler" in fallback.get_description()


def test_build_sampler_clamps_ratio_instead_of_raising():
    assert build_sampler("traceidratio", 1.7).rate == pytest.approx(1.0)
    assert build_sampler("traceidratio", -3.0).rate == pytest.approx(0.0)
    assert build_sampler("traceidratio", None).rate == pytest.approx(1.0)


# ══════════════════════════════════════════════════════════════════════
# 5. AgentMetrics：p50/p95 分位 + record_tokens
# ══════════════════════════════════════════════════════════════════════


def test_agent_metrics_p95_on_uniform_durations():
    metrics = AgentMetrics()
    for ms in range(1, 101):
        metrics.record_call("rag", float(ms), True)

    summary = metrics.get_summary()["rag"]
    assert summary["total_calls"] == 100
    assert summary["p95_duration_ms"] == pytest.approx(95.0)
    assert 50.0 <= summary["p50_duration_ms"] <= 52.0
    assert summary["avg_duration_ms"] == pytest.approx(50.5)
    assert summary["error_rate"] == 0.0


def test_agent_metrics_p95_is_zero_without_samples():
    metrics = AgentMetrics()
    assert metrics.get_summary() == {}
    assert AgentMetrics._percentile([], 0.95) == 0.0


def test_agent_metrics_record_tokens_aggregates_per_model_and_agent():
    metrics = AgentMetrics()
    metrics.record_call("rag", 5.0, True)
    metrics.record_tokens("gpt-5.1", 1200, 300, agent_name="rag")
    metrics.record_tokens("gpt-5.1", 800, 200, agent_name="rag")

    tokens = metrics.get_token_summary()["gpt-5.1"]
    assert tokens["calls"] == 2
    assert tokens["input_tokens"] == 2000
    assert tokens["output_tokens"] == 500
    assert tokens["total_tokens"] == 2500
    assert tokens["cost_usd"] == pytest.approx(estimate_cost("gpt-5.1", 2000, 500))

    summary = metrics.get_summary()["rag"]
    assert summary["input_tokens"] == 2000
    assert summary["output_tokens"] == 500
    assert summary["total_tokens"] == 2500
    assert summary["estimated_cost_usd"] == pytest.approx(tokens["cost_usd"])


# ══════════════════════════════════════════════════════════════════════
# 6. 既有调用方契约：api/main.py 的 init_tracer 形态保持可用
# ══════════════════════════════════════════════════════════════════════


def test_init_tracer_backward_compatible_signature():
    params = inspect.signature(otel.init_tracer).parameters
    for name, default in (
        ("service_name", "heteroagent"),
        ("sampler_name", "parentbased_always_on"),
        ("sampler_ratio", 1.0),
        ("console_export", False),
    ):
        assert name in params, name
        assert params[name].default == default, name
    assert "otlp_endpoint" in params
    assert any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ), "必须保留 **kwargs 以吸收历史参数"


def test_init_tracer_api_style_call_stays_offline(memory_tracer):
    # api/main.py lifespan 的调用形态：无 exporter、无 processor —— 不联网、不刷屏
    provider = otel.init_tracer(
        service_name="heteroagent",
        otlp_endpoint=None,
        sampler_name="always_on",
        console_export=False,
    )
    assert provider is not None
    assert otel.get_provider() is provider
    assert otel.get_tracer() is not None
    assert provider.sampler.get_description() == "AlwaysOnSampler"

    ratio_provider = otel.init_tracer(
        service_name="heteroagent",
        otlp_endpoint=None,
        sampler_name="traceidratio",
        sampler_ratio=0.5,
        console_export=False,
    )
    assert ratio_provider.sampler.rate == pytest.approx(0.5)

    # 历史遗留的关键字参数被静默吸收（不抛 TypeError，api 启动不崩）
    legacy = otel.init_tracer(
        service_name="heteroagent",
        otlp_endpoint=None,
        sampler_name="parentbased_always_on",
        console_export=False,
        sample_rate=0.3,
    )
    assert legacy is not None

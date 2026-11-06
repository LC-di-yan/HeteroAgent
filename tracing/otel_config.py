"""全链路追踪 —— OpenTelemetry GenAI 语义约定（T-P1-08）。

相对 P0 版（G13）的变化
----------------------
1. 属性语义对齐：span 属性由自研 "agent.*" 升级为官方 GenAI 语义约定 "gen_ai.*"；
   旧键（agent.name / agent.method / agent.duration_ms / agent.success）继续双写一个
   版本周期，既有看板、告警规则与测试不受影响，可平滑下线。
2. 分层 span 工厂：agent_span → "invoke_agent <name>"、inference_span → "chat <model>"、
   tool_span → "execute_tool <tool>"、retrieval_span → "retrieval <store>" / "embeddings"。
   父子关系由 OTel context 自动串联，后端可直接看到完整会话树。
3. token 与成本核算：record_usage() 写 usage 属性，按模块级价格表 PRICE_PER_1K 估算成本，
   并累积到线程安全的进程级 CostTracker（get_cost_summary()）。未知模型 token 照记、
   成本记 0 且 priced=False，绝不静默丢用量。
4. 指标口径补齐：AgentMetrics 增加 p50/p95/p99 分位与按模型、按 Agent 的 token/成本汇总
   （record_tokens / get_token_summary）。

兼容性契约（既有调用方零改动）
------------------------------
api/main.py：init_tracer(service_name=..., otlp_endpoint=..., sampler_name=...,
console_export=...)、set_global_metrics(...)、AgentMetrics().record_call(name, duration_ms,
success) / .get_summary()；
agents/*：@trace_agent_call("<name>") 装饰 async 方法；另有 get_tracer() / build_sampler()。

采样策略仍由调用方（Settings.otel_traces_sampler）传入，本模块不读配置层，避免追踪层
反向依赖 config，也保证单测可离线重复运行。
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_INSTRUMENTATION_NAME = "heteroagent.tracing"
_INSTRUMENTATION_VERSION = "1.0.0"

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )
    from opentelemetry.trace import SpanKind, Status, StatusCode

    _HAS_OTEL = True
except ImportError:  # pragma: no cover - 精简运行环境：追踪降级为纯指标计数
    _HAS_OTEL = False
    trace = None  # type: ignore[assignment]
    Resource = TracerProvider = BatchSpanProcessor = ConsoleSpanExporter = None  # type: ignore
    ALWAYS_OFF = ALWAYS_ON = ParentBased = TraceIdRatioBased = None  # type: ignore
    SpanKind = StatusCode = Status = None  # type: ignore


# ══════════════════════════════════════════════════════════════════════
# 一、GenAI 语义约定属性键（锁定 GenAI semconv 版本；升级时查 changelog）
# ══════════════════════════════════════════════════════════════════════

GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_FINISH_REASONS = "gen_ai.response.finish_reasons"
SESSION_ID = "session.id"

# 扩展键（GenAI semconv 未标准化，仍挂 gen_ai.* 前缀便于后端聚合）
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_RESPONSE_ID = "gen_ai.response.id"
GEN_AI_COST_USD = "gen_ai.usage.cost_usd"
GEN_AI_USAGE_CURRENCY = "gen_ai.usage.currency"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"

# 检索层属性（4.5.1 表格：store / top_k / 结果数 / 最高分）
RETRIEVAL_STORE = "retrieval.store"
RETRIEVAL_TOP_K = "retrieval.top_k"
RETRIEVAL_QUERY = "retrieval.query"
RETRIEVAL_QUERY_LENGTH = "retrieval.query_length"
RETRIEVAL_RESULT_COUNT = "retrieval.result_count"
RETRIEVAL_SCORE_MAX = "retrieval.score_max"

# 旧属性键（双写一个版本周期，T-P1-08 兼容条款）
LEGACY_AGENT_NAME = "agent.name"
LEGACY_AGENT_METHOD = "agent.method"
LEGACY_DURATION_MS = "agent.duration_ms"
LEGACY_SUCCESS = "agent.success"
LEGACY_ERROR_TYPE = "agent.error_type"

# 通用 span 耗时（自研工厂写入；装饰器另写 agent.duration_ms 兼容旧键）
ATTR_DURATION_MS = "duration_ms"

# gen_ai.operation.name 取值
OP_INVOKE_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"
OP_EMBEDDINGS = "embeddings"
OP_RETRIEVAL = "retrieval"

DEFAULT_PROVIDER = "openai"
UNKNOWN_MODEL = "unknown"
COST_CURRENCY = "USD"
# 默认不落盘查询原文（4.5.1「默认不采集提示词全文」）
DEFAULT_RECORD_QUERY = False

# 模型价格表：model -> (每 1K 输入 token 单价, 每 1K 输出 token 单价)，单位 USD。
# 表外模型：token 数照记，成本记 0 且 priced=False（显式「未计价」而非漏账）。
PRICE_PER_1K: dict[str, tuple[float, float]] = {
    "gpt-5.1": (0.00125, 0.01),
    "gpt-5.1-mini": (0.00025, 0.002),
    "gpt-5.1-nano": (0.0001, 0.001),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.0006, 0.0024),
    "o4-mini": (0.0011, 0.0044),
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
    "qwen3-max": (0.0016, 0.0064),
    "deepseek-v3": (0.00027, 0.0011),
}
# 供应商推断（仅用于 gen_ai.provider.name 归类，不触发任何网络行为）
_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("text-embedding", "openai"),
    ("claude", "anthropic"),
    ("gemini", "gcp.gemini"),
    ("qwen", "qwen"),
    ("deepseek", "deepseek"),
    ("bge", "hugging_face"),
)


# ══════════════════════════════════════════════════════════════════════
# 二、模块状态（provider / tracer / 全局指标）
# ══════════════════════════════════════════════════════════════════════

_provider: Any = None          # 本模块安装的活动 TracerProvider
_tracer: Any = None            # 活动 tracer（缓存自 _tracer_provider）
_tracer_provider: Any = None   # _tracer 来自哪个 provider，用于识别换装
_tracer_scope: str = _INSTRUMENTATION_NAME
_global_metrics: Any = None
_state_lock = threading.RLock()

_SAMPLERS: dict[str, Callable[[float], Any]] = {
    "always_on": lambda ratio: ALWAYS_ON,
    "always_off": lambda ratio: ALWAYS_OFF,
    "parentbased_always_on": lambda ratio: ParentBased(ALWAYS_ON),
    "parentbased_always_off": lambda ratio: ParentBased(ALWAYS_OFF),
    "traceidratio": lambda ratio: TraceIdRatioBased(ratio),
    "parentbased_traceidratio": lambda ratio: ParentBased(TraceIdRatioBased(ratio)),
}


def build_sampler(name: str, ratio: float = 1.0) -> Any:
    """把 OTEL_TRACES_SAMPLER 字符串映射为 SDK Sampler。

    支持 6 种官方取值；未知/空值回退 parentbased_always_on（宁多采不漏采）。
    ratio 夹到 [0,1]，避免 TraceIdRatioBased 抛 ValueError 拖垮应用启动。
    """
    if not _HAS_OTEL:
        return None
    try:
        bounded = 1.0 if ratio is None else max(0.0, min(1.0, float(ratio)))
    except (TypeError, ValueError):
        bounded = 1.0
    factory = _SAMPLERS.get((name or "").strip().lower(), _SAMPLERS["parentbased_always_on"])
    try:
        return factory(bounded)
    except Exception:  # pragma: no cover - 防御 SDK 变更
        return _SAMPLERS["parentbased_always_on"](1.0)


def get_provider() -> Any:
    """本模块当前安装的活动 provider（未安装时为 None）。"""
    return _provider


def install_provider(provider: Any, *, service_name: str | None = None) -> Any:
    """安装活动 provider 并刷新模块 tracer，返回被替换掉的旧 provider。

    与 trace.set_tracer_provider() 的差异：全局 provider 只允许设置一次，而本模块
    以显式传入的 provider 为准，因此测试可反复注入 InMemorySpanExporter provider，
    不受进程内其它组件先占全局位的影响。
    """
    global _provider, _tracer, _tracer_provider, _tracer_scope
    with _state_lock:
        previous = _provider
        _provider = provider
        _tracer = None
        _tracer_provider = None
        _tracer_scope = service_name or _INSTRUMENTATION_NAME
        if _HAS_OTEL and provider is not None:
            try:
                trace.set_tracer_provider(provider)
            except Exception as exc:  # pragma: no cover - 全局位已被占用
                logger.debug("全局 TracerProvider 已存在，本模块沿用传入 provider: %s", exc)
            _tracer = provider.get_tracer(_tracer_scope, _INSTRUMENTATION_VERSION)
            _tracer_provider = provider
        return previous


def reset_tracing() -> None:
    """解绑本模块的 provider/tracer（测试隔离用；不影响已导出的历史 span）。"""
    global _provider, _tracer, _tracer_provider, _tracer_scope
    with _state_lock:
        _provider = None
        _tracer = None
        _tracer_provider = None
        _tracer_scope = _INSTRUMENTATION_NAME


def get_tracer(name: str | None = None) -> Any:
    """返回活动 tracer；未显式安装 provider 时按全局 provider 惰性创建。

    OTel 不可用时返回 None，所有 span 工厂退化为 no-op（调用方无需分支判断）。
    """
    global _tracer, _tracer_provider, _tracer_scope
    if not _HAS_OTEL:
        return None
    with _state_lock:
        provider = _provider if _provider is not None else trace.get_tracer_provider()
        scope = name or _tracer_scope
        if _tracer is None or provider is not _tracer_provider or scope != _tracer_scope:
            _tracer = provider.get_tracer(scope, _INSTRUMENTATION_VERSION)
            _tracer_provider = provider
            _tracer_scope = scope
        return _tracer


def set_global_metrics(metrics: Any) -> None:
    """注册全局指标收集器（api/main.py 在 lifespan 里调用；传 None 可解除）。"""
    global _global_metrics
    _global_metrics = metrics


def get_global_metrics() -> Any:
    """当前生效的全局指标收集器。"""
    return _global_metrics


def init_tracer(
    service_name: str = "heteroagent",
    otlp_endpoint: str | None = None,
    sampler_name: str = "parentbased_always_on",
    sampler_ratio: float = 1.0,
    console_export: bool = False,
    *,
    resource_attributes: Mapping[str, Any] | None = None,
    exporter: Any = None,
    span_processor: Any = None,
    instrument_fastapi: bool = False,
    **legacy_kwargs: Any,
) -> Any:
    """初始化追踪器（前 5 个参数与 P0 版完全一致，新增项全部带默认值）。

    - otlp_endpoint 有值 -> OTLP 导出（构造 exporter 不联网，失败只告警）；
    - 否则 console_export=True -> 控制台导出；
    - 都没有 -> 只保留进程内 span（不刷屏）；
    - sampler_ratio：均匀采样比（0~1），交给 build_sampler 落地；
    - exporter / span_processor / resource_attributes：测试与部署侧精调用；
    - instrument_fastapi=True：提示可接官方 HTTP 自动插桩（应用对象上需显式调用
      instrument_fastapi_app()，默认关闭以免隐式改变 API 行为）。

    返回创建的 TracerProvider（OTel 不可用时返回 None）。
    """
    if not _HAS_OTEL:  # pragma: no cover - 依赖缺失时的降级路径
        logger.warning("opentelemetry 不可用，追踪降级为进程内指标计数")
        return None
    if legacy_kwargs:
        logger.debug("init_tracer 忽略的历史参数: %s", sorted(legacy_kwargs))

    attrs: dict[str, Any] = {"service.name": service_name}
    if resource_attributes:
        attrs.update(dict(resource_attributes))

    provider = TracerProvider(
        resource=Resource.create(attrs),
        sampler=build_sampler(sampler_name, sampler_ratio),
    )

    processor = span_processor
    if processor is None:
        chosen = exporter
        if chosen is None and otlp_endpoint:
            chosen = _build_otlp_exporter(otlp_endpoint)
        if chosen is None and console_export:
            chosen = ConsoleSpanExporter()
        if chosen is not None:
            processor = BatchSpanProcessor(chosen)
    if processor is not None:
        provider.add_span_processor(processor)

    install_provider(provider, service_name=service_name)
    logger.info(
        "OTel 初始化: service=%s sampler=%s ratio=%s processor=%s",
        service_name,
        sampler_name,
        sampler_ratio,
        type(processor).__name__ if processor is not None else "none",
    )
    return provider


def _build_otlp_exporter(endpoint: str) -> Any:
    """按 endpoint 构造 OTLP exporter：gRPC 优先、HTTP 兜底，全失败返回 None。"""
    for module_path, class_name in (
        ("opentelemetry.exporter.otlp.proto.grpc.trace_exporter", "OTLPSpanExporter"),
        ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "OTLPSpanExporter"),
    ):
        try:
            module = __import__(module_path, fromlist=[class_name])
            return getattr(module, class_name)(endpoint=endpoint)
        except Exception as exc:  # pragma: no cover - 依赖或协议不可用
            logger.warning("OTLP exporter(%s) 初始化失败: %s", module_path, exc)
    return None


def instrument_fastapi_app(app: Any) -> Any:
    """可选接线：对 FastAPI 应用启用官方自动插桩，得到 HTTP 入口 span。

    现有 api/main.py 未调用（T-P1-08 不动 API 层）；需要时在 create_app() 内加一行
    instrument_fastapi_app(app)，HTTP span 会与 gen_ai.* span 自动组成完整链路树。
    重复调用安全（内部判重）。
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            return app
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # pragma: no cover - 插桩包缺失
        logger.warning("FastAPI 自动插桩不可用: %s", exc)
    return app


# ══════════════════════════════════════════════════════════════════════
# 三、span 属性写入与分层 span 工厂
# ══════════════════════════════════════════════════════════════════════


class _NoopSpan:
    """OTel 缺席时的 span 替身：所有写入都是空操作，业务逻辑不受影响。"""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> _NoopSpan:
        return self

    def set_attributes(self, attributes: Mapping[str, Any]) -> _NoopSpan:
        return self

    def add_event(self, name: str, attributes: Any = None, timestamp: Any = None) -> _NoopSpan:
        return self

    def record_exception(self, exception: BaseException, attributes: Any = None) -> _NoopSpan:
        return self

    def set_status(self, status: Any, description: str | None = None) -> _NoopSpan:
        return self

    def get_span_context(self) -> Any:
        return None

    def is_recording(self) -> bool:
        return False

    def end(self, end_time: Any = None) -> None:
        return None


_NOOP_SPAN = _NoopSpan()


def set_attributes(span: Any, attributes: Mapping[str, Any] | None) -> None:
    """把 attributes 写入 span：None 值跳过，序列统一为字符串数组。

    单条属性写入失败只 debug 记录，绝不让追踪打断业务调用。
    """
    if span is None or not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = [v if isinstance(v, str) else str(v) for v in value]
        elif not isinstance(value, (bool, int, float, str)):
            value = str(value)
        try:
            span.set_attribute(key, value)
        except Exception as exc:  # pragma: no cover - SDK 版本差异防御
            logger.debug("span 属性写入失败 %s: %s", key, exc)


def _span_attr(span: Any, key: str) -> Any:
    """尽力读取 span 上已有的属性值（record_usage 用它反查模型名）。"""
    if span is None:
        return None
    try:
        attributes = getattr(span, "attributes", None)
        if attributes:
            return attributes.get(key)
    except Exception:  # pragma: no cover
        return None
    return None


@contextmanager
def _start_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    kind: Any = None,
) -> Iterator[Any]:
    """分层 span 的公共生命周期：写属性 -> yield -> 补耗时 -> 异常交 SDK 落 ERROR。"""
    tracer = get_tracer()
    if tracer is None:
        yield _NOOP_SPAN
        return
    start = time.perf_counter()
    with tracer.start_as_current_span(name, kind=kind or SpanKind.INTERNAL) as span:
        set_attributes(span, attributes)
        try:
            yield span
        finally:
            # finally 仍在 span 结束前执行，成功/异常两条路径都能看到耗时
            set_attributes(span, {ATTR_DURATION_MS: round((time.perf_counter() - start) * 1000, 3)})


@contextmanager
def agent_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    session_id: str | None = None,
    provider: str | None = None,
    operation: str = OP_INVOKE_AGENT,
) -> Iterator[Any]:
    """"invoke_agent <name>"：子 Agent 调用层（可嵌套 inference / tool / retrieval）。"""
    attrs: dict[str, Any] = {
        GEN_AI_OPERATION: operation,
        GEN_AI_AGENT_NAME: name,
        LEGACY_AGENT_NAME: name,  # 双写一个版本周期
    }
    if provider:
        attrs[GEN_AI_PROVIDER] = provider
    if session_id:
        attrs[SESSION_ID] = session_id
    attrs.update(attributes or {})
    with _start_span(operation + " " + name, attrs) as span:
        yield span


@contextmanager
def inference_span(
    model: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    provider: str | None = None,
    response_model: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    operation: str = OP_CHAT,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Iterator[Any]:
    """"chat <model>"：一次模型推理。token 与成本在体内调 record_usage() 记账。"""
    attrs: dict[str, Any] = {
        GEN_AI_OPERATION: operation,
        GEN_AI_REQUEST_MODEL: model,
        GEN_AI_PROVIDER: provider or infer_provider(model),
    }
    if response_model:
        attrs[GEN_AI_RESPONSE_MODEL] = response_model
    if request_id:
        attrs[GEN_AI_RESPONSE_ID] = request_id
    if session_id:
        attrs[SESSION_ID] = session_id
    if temperature is not None:
        attrs[GEN_AI_REQUEST_TEMPERATURE] = float(temperature)
    if max_tokens is not None:
        attrs[GEN_AI_REQUEST_MAX_TOKENS] = int(max_tokens)
    attrs.update(attributes or {})
    with _start_span(operation + " " + model, attrs) as span:
        yield span


@contextmanager
def tool_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    arg_size: int | None = None,
) -> Iterator[Any]:
    """"execute_tool <name>"：MCP / LangChain 工具执行。

    只记参数字节数（tool.arg_size），不记参数原文（4.5.1 隐私条款）。
    """
    attrs: dict[str, Any] = {GEN_AI_OPERATION: OP_EXECUTE_TOOL, GEN_AI_TOOL_NAME: name}
    if tool_call_id:
        attrs[GEN_AI_TOOL_CALL_ID] = tool_call_id
    if session_id:
        attrs[SESSION_ID] = session_id
    if arg_size is not None:
        attrs["tool.arg_size"] = int(arg_size)
    attrs.update(attributes or {})
    with _start_span(OP_EXECUTE_TOOL + " " + name, attrs) as span:
        yield span


@contextmanager
def retrieval_span(
    store: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    *,
    operation: str = OP_RETRIEVAL,
    model: str | None = None,
    query: str | None = None,
    top_k: int | None = None,
    session_id: str | None = None,
    record_query: bool = DEFAULT_RECORD_QUERY,
) -> Iterator[Any]:
    """"retrieval <store>"（向量检索）或 "embeddings"（嵌入）。

    operation="embeddings" 时 span 名取 embeddings，配合 model 记录嵌入模型；
    operation="retrieval" 且给出 store 时 span 名取 "retrieval <store>"。
    默认只记查询长度，record_query=True 才落原文。
    """
    op = (operation or OP_RETRIEVAL).strip().lower()
    if op in (OP_EMBEDDINGS, "embedding"):
        op = OP_EMBEDDINGS
        span_name = OP_EMBEDDINGS
    else:
        span_name = (OP_RETRIEVAL + " " + store) if store else OP_RETRIEVAL
    attrs: dict[str, Any] = {GEN_AI_OPERATION: op}
    if store:
        attrs[RETRIEVAL_STORE] = store
    if model:
        attrs[GEN_AI_REQUEST_MODEL] = model
    if top_k is not None:
        attrs[RETRIEVAL_TOP_K] = int(top_k)
    if query is not None:
        attrs[RETRIEVAL_QUERY_LENGTH] = len(str(query))
        if record_query:
            attrs[RETRIEVAL_QUERY] = str(query)
    if session_id:
        attrs[SESSION_ID] = session_id
    attrs.update(attributes or {})
    with _start_span(span_name, attrs) as span:
        yield span


def infer_provider(model: str | None) -> str:
    """按模型名前缀推断 gen_ai.provider.name；无法判定时用默认供应商。"""
    name = (model or "").strip().lower()
    if not name:
        return DEFAULT_PROVIDER
    for prefix, provider in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return provider
    return DEFAULT_PROVIDER


# ══════════════════════════════════════════════════════════════════════
# 四、token 用量与成本核算
# ══════════════════════════════════════════════════════════════════════


def price_for(model: str | None) -> tuple[float, float] | None:
    """返回 (每1K输入单价, 每1K输出单价)；表外模型返回 None。大小写不敏感。"""
    key = (model or "").strip().lower()
    if not key:
        return None
    return PRICE_PER_1K.get(key)


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """按 PRICE_PER_1K 估算成本（USD）。未知模型返回 0.0（用量不丢）。"""
    prices = price_for(model)
    if prices is None:
        return 0.0
    in_price, out_price = prices
    it = max(0, int(input_tokens))
    ot = max(0, int(output_tokens))
    return round(it / 1000.0 * in_price + ot / 1000.0 * out_price, 10)


def _model_key(model: str | None) -> str:
    return (model or "").strip() or UNKNOWN_MODEL


def _blank_usage_entry(priced: bool = True) -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "priced": priced,
        "currency": COST_CURRENCY,
    }


class CostTracker:
    """线程安全的进程级 token/成本累加器（多 Agent 并发调用同一 provider）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_model: dict[str, dict[str, Any]] = {}

    def add(
        self,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
        *,
        priced: bool = True,
    ) -> dict[str, Any]:
        """累加一次用量，返回累加后的该模型快照副本。"""
        key = _model_key(model)
        it = max(0, int(input_tokens or 0))
        ot = max(0, int(output_tokens or 0))
        with self._lock:
            entry = self._by_model.get(key)
            if entry is None:
                entry = _blank_usage_entry(priced)
                self._by_model[key] = entry
            entry["calls"] += 1
            entry["input_tokens"] += it
            entry["output_tokens"] += ot
            entry["total_tokens"] += it + ot
            entry["cost_usd"] = round(entry["cost_usd"] + float(cost_usd), 10)
            entry["priced"] = bool(entry["priced"]) and bool(priced)
            return dict(entry)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {model: dict(entry) for model, entry in self._by_model.items()}

    def total_cost_usd(self) -> float:
        with self._lock:
            return round(sum(entry["cost_usd"] for entry in self._by_model.values()), 10)

    def reset(self) -> None:
        with self._lock:
            self._by_model.clear()


_COST_TRACKER = CostTracker()


def get_cost_tracker() -> CostTracker:
    """全局成本累加器实例（需要 total_cost_usd() 等扩展口径时使用）。"""
    return _COST_TRACKER


def get_cost_summary() -> dict[str, dict[str, Any]]:
    """按模型汇总 token 与成本：{model: {calls, input_tokens, output_tokens, cost_usd, ...}}。"""
    return _COST_TRACKER.snapshot()


def reset_cost_tracker() -> None:
    """清空全局成本累加（测试隔离、对账重算）。"""
    _COST_TRACKER.reset()


def record_usage(
    span: Any,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    model: str | None = None,
    response_model: str | None = None,
    provider: str | None = None,
    finish_reasons: Any = None,
    track_cost: bool = True,
    metrics: Any = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """把 token 用量写入 span，估算成本并累积到全局 CostTracker。

    模型解析顺序：显式 model -> response_model -> span 上的 gen_ai.request.model ->
    span 上的 gen_ai.response.model -> "unknown"。未知模型 token 照记、成本记 0，
    返回值 priced 为 False。

    返回 {model, input_tokens, output_tokens, total_tokens, cost_usd, priced, currency}。
    """
    it = max(0, int(input_tokens or 0))
    ot = max(0, int(output_tokens or 0))
    resolved = (
        model
        or response_model
        or _span_attr(span, GEN_AI_REQUEST_MODEL)
        or _span_attr(span, GEN_AI_RESPONSE_MODEL)
        or UNKNOWN_MODEL
    )
    resolved = _model_key(resolved)
    cost = estimate_cost(resolved, it, ot)
    priced = price_for(resolved) is not None

    attrs: dict[str, Any] = {
        GEN_AI_INPUT_TOKENS: it,
        GEN_AI_OUTPUT_TOKENS: ot,
        GEN_AI_TOTAL_TOKENS: it + ot,
        GEN_AI_COST_USD: cost,
        GEN_AI_USAGE_CURRENCY: COST_CURRENCY,
    }
    if resolved != UNKNOWN_MODEL:
        attrs[GEN_AI_REQUEST_MODEL] = resolved
        if response_model:
            attrs[GEN_AI_RESPONSE_MODEL] = response_model
        attrs[GEN_AI_PROVIDER] = provider or infer_provider(resolved)
    if finish_reasons:
        reasons = (
            list(finish_reasons) if isinstance(finish_reasons, (list, tuple)) else [str(finish_reasons)]
        )
        attrs[GEN_AI_FINISH_REASONS] = reasons
    set_attributes(span, attrs)

    if track_cost:
        _COST_TRACKER.add(resolved, it, ot, cost, priced=priced)
    if metrics is None:
        metrics = _global_metrics
    recorder = getattr(metrics, "record_tokens", None)
    if callable(recorder):
        try:
            recorder(resolved, it, ot, agent_name=agent_name)
        except TypeError:  # pragma: no cover - 兼容无 agent_name 形参的旧指标器
            recorder(resolved, it, ot)
        except Exception as exc:  # pragma: no cover
            logger.debug("token 指标记录失败: %s", exc)

    return {
        "model": resolved,
        "input_tokens": it,
        "output_tokens": ot,
        "total_tokens": it + ot,
        "cost_usd": cost,
        "priced": priced,
        "currency": COST_CURRENCY,
    }


def record_retrieval_result(
    span: Any,
    *,
    result_count: int | None = None,
    top_score: float | None = None,
    top_k: int | None = None,
) -> None:
    """补写检索结果指标（结果数 / 最高分），供「检索质量 ↔ 成本」联合下钻。"""
    attrs: dict[str, Any] = {}
    if result_count is not None:
        attrs[RETRIEVAL_RESULT_COUNT] = int(result_count)
    if top_score is not None:
        attrs[RETRIEVAL_SCORE_MAX] = float(top_score)
    if top_k is not None:
        attrs[RETRIEVAL_TOP_K] = int(top_k)
    set_attributes(span, attrs)


# ══════════════════════════════════════════════════════════════════════
# 五、旧装饰器（双写一个版本周期）
# ══════════════════════════════════════════════════════════════════════


def _record_metrics(agent_name: str, duration_ms: float, success: bool) -> None:
    metrics = _global_metrics
    if metrics is None:
        return
    try:
        metrics.record_call(agent_name, duration_ms, success)
    except Exception as exc:  # pragma: no cover - 指标器异常不影响业务
        logger.debug("Agent 指标记录失败: %s", exc)


def _infer_session_id(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str | None:
    """从入参里尽力取 session.id：显式 kwarg 优先，其次任何含 session_id 的 mapping
    （Agent 方法的 state 字典）。取不到就留空，不做猜测。"""
    direct = kwargs.get("session_id") or kwargs.get("session")
    if isinstance(direct, str) and direct:
        return direct
    for value in list(args) + list(kwargs.values()):
        if isinstance(value, Mapping):
            candidate = value.get("session_id")
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def trace_agent_call(agent_name: str) -> Callable:
    """为 async Agent 方法创建 span 并上报耗时指标（向后兼容保留一个版本周期）。

    - span 名：invoke_agent <agent_name>.<method>（旧版是 agent.<name>.<method>）；
    - 新属性：gen_ai.operation.name="invoke_agent"、gen_ai.agent.name、session.id；
    - 旧属性：agent.name / agent.method / agent.duration_ms / agent.success 继续双写，
      异常路径另写 agent.error_type；
    - 异常交 SDK 自动 record_exception + StatusCode.ERROR，指标记为失败。
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            method = getattr(func, "__name__", "unknown")
            start = time.perf_counter()

            if tracer is None:
                # OTel 不可用：跳过 span，但指标口径保持不变
                success = True
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    success = False
                    raise
                finally:
                    _record_metrics(agent_name, (time.perf_counter() - start) * 1000, success)

            attrs: dict[str, Any] = {
                GEN_AI_OPERATION: OP_INVOKE_AGENT,
                GEN_AI_AGENT_NAME: agent_name,
                LEGACY_AGENT_NAME: agent_name,
                LEGACY_AGENT_METHOD: method,
            }
            session_id = _infer_session_id(args, kwargs)
            if session_id:
                attrs[SESSION_ID] = session_id

            with _start_span(OP_INVOKE_AGENT + " " + agent_name + "." + method, attrs) as span:
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    duration_ms = (time.perf_counter() - start) * 1000
                    set_attributes(
                        span,
                        {
                            LEGACY_DURATION_MS: duration_ms,
                            LEGACY_SUCCESS: False,
                            LEGACY_ERROR_TYPE: type(exc).__name__,
                        },
                    )
                    _record_metrics(agent_name, duration_ms, False)
                    raise
                duration_ms = (time.perf_counter() - start) * 1000
                set_attributes(span, {LEGACY_DURATION_MS: duration_ms, LEGACY_SUCCESS: True})
                _record_metrics(agent_name, duration_ms, True)
                return result

        return wrapper

    return decorator


# ══════════════════════════════════════════════════════════════════════
# 六、进程内指标（/metrics 兼容视图）
# ══════════════════════════════════════════════════════════════════════


class AgentMetrics:
    """Agent 调用指标收集器（进程内、线程安全）。

    T-P1-08 阶段保留为 /metrics 的开发态调试视图：延迟分位（p50/p95/p99）+ 错误率 +
    按模型 / 按 Agent 的 token 与成本。OTel metrics 直方图迁移在 T-P2 评估闭环完成，
    届时本类降级为纯调试输出。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._call_counts: dict[str, int] = {}
        self._total_duration: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}
        self._durations: dict[str, list[float]] = {}
        self._tokens_by_model: dict[str, dict[str, Any]] = {}
        self._tokens_by_agent: dict[str, dict[str, Any]] = {}

    # ── 写入 ──
    def record_call(self, agent_name: str, duration_ms: float, success: bool) -> None:
        with self._lock:
            self._call_counts[agent_name] = self._call_counts.get(agent_name, 0) + 1
            self._total_duration[agent_name] = self._total_duration.get(agent_name, 0.0) + duration_ms
            self._durations.setdefault(agent_name, []).append(duration_ms)
            if not success:
                self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    def record_tokens(
        self,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        *,
        agent_name: str | None = None,
    ) -> None:
        """记录一次调用的 token 用量（按模型聚合，可选按 Agent 归因）。"""
        it = max(0, int(input_tokens or 0))
        ot = max(0, int(output_tokens or 0))
        key = _model_key(model)
        cost = estimate_cost(key, it, ot)
        priced = price_for(key) is not None
        with self._lock:
            entries = [self._tokens_by_model.setdefault(key, _blank_usage_entry(priced))]
            if agent_name is not None:
                entries.append(self._tokens_by_agent.setdefault(agent_name, _blank_usage_entry(priced)))
            for entry in entries:
                entry["calls"] += 1
                entry["input_tokens"] += it
                entry["output_tokens"] += ot
                entry["total_tokens"] += it + ot
                entry["cost_usd"] = round(entry["cost_usd"] + cost, 10)

    # ── 读取 ──
    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        """最近秩分位：样本 1..100 的 p95 -> 95.0；空样本返回 0.0。"""
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return ordered[idx]

    def get_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        with self._lock:
            for name, calls in self._call_counts.items():
                durations = self._durations.get(name, [])
                entry: dict[str, Any] = {
                    "total_calls": calls,
                    "avg_duration_ms": self._total_duration[name] / calls if calls else 0.0,
                    "p50_duration_ms": self._percentile(durations, 0.50),
                    "p95_duration_ms": self._percentile(durations, 0.95),
                    "p99_duration_ms": self._percentile(durations, 0.99),
                    "error_rate": self._error_counts.get(name, 0) / calls if calls else 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                }
                tokens = self._tokens_by_agent.get(name)
                if tokens is not None:
                    entry["input_tokens"] = tokens["input_tokens"]
                    entry["output_tokens"] = tokens["output_tokens"]
                    entry["total_tokens"] = tokens["total_tokens"]
                    entry["estimated_cost_usd"] = tokens["cost_usd"]
                summary[name] = entry
        return summary

    def get_token_summary(self) -> dict[str, Any]:
        """按模型的 token/成本汇总（口径与 get_cost_summary() 一致）。"""
        with self._lock:
            return {model: dict(entry) for model, entry in self._tokens_by_model.items()}

    def total_cost_usd(self) -> float:
        with self._lock:
            return round(sum(e["cost_usd"] for e in self._tokens_by_model.values()), 10)


__all__ = [
    # 属性键
    "GEN_AI_OPERATION",
    "GEN_AI_PROVIDER",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_RESPONSE_MODEL",
    "GEN_AI_INPUT_TOKENS",
    "GEN_AI_OUTPUT_TOKENS",
    "GEN_AI_TOTAL_TOKENS",
    "GEN_AI_AGENT_NAME",
    "GEN_AI_TOOL_NAME",
    "GEN_AI_FINISH_REASONS",
    "SESSION_ID",
    "GEN_AI_TOOL_CALL_ID",
    "GEN_AI_RESPONSE_ID",
    "GEN_AI_COST_USD",
    "RETRIEVAL_STORE",
    "RETRIEVAL_TOP_K",
    "RETRIEVAL_RESULT_COUNT",
    "RETRIEVAL_SCORE_MAX",
    "OP_INVOKE_AGENT",
    "OP_CHAT",
    "OP_EXECUTE_TOOL",
    "OP_EMBEDDINGS",
    "OP_RETRIEVAL",
    "PRICE_PER_1K",
    # 生命周期
    "init_tracer",
    "get_tracer",
    "get_provider",
    "install_provider",
    "reset_tracing",
    "build_sampler",
    "instrument_fastapi_app",
    # span 工厂
    "agent_span",
    "inference_span",
    "tool_span",
    "retrieval_span",
    "set_attributes",
    "infer_provider",
    # 用量与成本
    "CostTracker",
    "record_usage",
    "record_retrieval_result",
    "estimate_cost",
    "price_for",
    "get_cost_summary",
    "get_cost_tracker",
    "reset_cost_tracker",
    # 兼容层
    "trace_agent_call",
    "set_global_metrics",
    "get_global_metrics",
    "AgentMetrics",
]

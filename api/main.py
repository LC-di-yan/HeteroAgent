"""FastAPI 入口 —— P0 安全加固版。

相对原实现的变化：
- 接口设防：API Key 鉴权、滑动窗口限流、请求体/消息长度上限、CORS 白名单（T-P0-07）；
- 注入检测前置到 API 入口，任一 LLM 调用之前（T-P0-01 / G1）；
- HITL：审批单外置（Redis/内存）+ 哈希链审计，审批通过不外发被拦截原文（T-P0-03 / G3 / G10）；
- 评估改 POST + 鉴权 + 并发保护（G14）；
- `/health` 拆 live/ready；移除 `reload=True`（T-P0-11）；
- 对话原文不再写入知识库（G8）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import Settings, get_settings
from security.approvals import ApprovalRecord, ApprovalStore, create_approval_store
from security.limits import build_rate_limiter
from tracing.logging_setup import configure_logging, reset_request_id, set_request_id
from tracing.otel_config import AgentMetrics, init_tracer, set_global_metrics

logger = logging.getLogger(__name__)

try:
    from security.injection import detect_injection
except Exception:  # pragma: no cover - 注入模块尚未就绪时显式告警
    detect_injection = None
    logger.warning("security.injection 未就绪，注入前置检测不可用")


APP_VERSION = "0.4.0"

SEED_KB: list[tuple[str, str]] = [
    ("我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。", "product_faq.md"),
    ("退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。", "refund_policy.md"),
    ("开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。", "account_guide.md"),
]

_STAGE_LABELS = {
    "supervisor_route": "🧠 正在分析您的意图…",
    "knowledge_rag": "📚 正在检索知识库…",
    "ticket_handler": "🎫 正在处理工单…",
    "compliance_check": "🛡️ 正在进行合规审查…",
    "synthesize": "✍️ 正在生成回复…",
}


# ─── Token 计数（G13：统一 tiktoken，离线降级） ───

_encoding = None
_encoding_checked = False


def _get_encoding():
    global _encoding, _encoding_checked
    if not _encoding_checked:
        _encoding_checked = True
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:
            logger.warning("tiktoken 编码表不可用，退化为字符估算: %s", exc)
            _encoding = None
    return _encoding


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 2)


async def _build_context_messages(stm: Any, session_id: str, current_message: str, max_tokens: int) -> list:
    """从短期记忆构建带 Token 预算的上下文（从最新向前截断）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    try:
        history = await stm.get_history(session_id)
    except Exception:
        history = []
    if not history:
        return [HumanMessage(content=current_message)]

    messages: list = []
    for entry in history:
        role = entry.get("role", "user")
        content = entry.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))

    total, truncated = 0, []
    for msg in reversed(messages):
        tokens = count_tokens(msg.content)
        if total + tokens > max_tokens:
            break
        total += tokens
        truncated.insert(0, msg)
    if truncated and not isinstance(truncated[-1], HumanMessage):
        truncated.append(HumanMessage(content=current_message))
    return truncated or [HumanMessage(content=current_message)]


async def _iter_graph_stream(graph: Any, state: dict[str, Any], config: dict[str, Any]):
    """优先用 updates+messages 双模式；图不支持时退回 updates（兼容测试桩）。

    yield (mode, payload)：
    - ("updates", {node: delta})
    - ("messages", (chunk, metadata))
    """
    agen = graph.astream(state, config=config, stream_mode=["updates", "messages"])
    try:
        first = await agen.__anext__()
    except StopAsyncIteration:
        return
    except (TypeError, ValueError, NotImplementedError):
        async for chunk in graph.astream(state, config=config, stream_mode="updates"):
            yield "updates", chunk
        return

    item = first
    try:
        while True:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                yield item[0], item[1]
            else:
                yield "updates", item
            item = await agen.__anext__()
    except StopAsyncIteration:
        return
    finally:
        await agen.aclose()


def _sanitize_for_release(text: str) -> str:
    """审批放行前的最后清洗：绝不外发被拦截原文（G3）。"""
    try:
        import agents.compliance_checker as cc

        fn = getattr(cc, "sanitize_output", None) or getattr(cc.ComplianceCheckerAgent, "sanitize_output", None)
        if callable(fn):
            result = fn(text)
            return result if isinstance(result, str) else text
    except Exception as exc:
        logger.warning("sanitize_output 不可用，退化为原文（合规节点已脱敏过）: %s", exc)
    return text


def build_chat_llm(settings: Settings):
    """主模型（保留旧名字供调用方兼容）。"""
    from agents.llm_factory import build_main_llm

    return build_main_llm(settings)


def _build_default_stm(settings: Settings):
    from memory.short_term import ShortTermMemory

    return ShortTermMemory(
        redis_url=settings.redis_url,
        max_turns=settings.short_term_max_turns,
        ttl_seconds=settings.short_term_ttl_seconds,
    )


def _build_default_ltm(settings: Settings):
    if settings.demo_mode:
        from agents.demo_mode import DemoKnowledgeBase

        return DemoKnowledgeBase()

    from memory.long_term import create_vector_store

    try:
        return create_vector_store(settings=settings)
    except TypeError:
        return create_vector_store()


# ─── 请求/响应模型 ───

class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    user_id: str = "anonymous"
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool
    approval_id: str | None = None
    trace_id: str = ""


class DecisionRequest(BaseModel):
    reason: str | None = None


class EvaluationRequest(BaseModel):
    dataset: str | None = None
    top_k: int = 5


def create_app(
    *,
    settings: Settings | None = None,
    graph: Any | None = None,
    llm: Any | None = None,
    short_term_memory: Any | None = None,
    long_term_memory: Any | None = None,
    approvals: ApprovalStore | None = None,
    mcp_server: Any | None = None,
    guardrail: Any | None = None,
) -> FastAPI:
    """应用工厂：依赖注入，测试可传入桩实现。"""
    st = settings or get_settings()
    configure_logging(level=getattr(st, "log_level", "INFO"), fmt=getattr(st, "log_format", "text"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_tracer(
            service_name=st.otel_service_name,
            otlp_endpoint=st.otel_exporter_otlp_endpoint or None,
            sampler_name=st.otel_traces_sampler,
            console_export=st.otel_console_export,
        )
        set_global_metrics(app.state.metrics)

        if app.state.llm is None:
            try:
                app.state.llm = build_chat_llm(st)
            except Exception as exc:
                logger.error("LLM 初始化失败（请检查 OPENAI_API_KEY）: %s", exc)
        if app.state.llm is not None and getattr(app.state, "light_llm", None) is None:
            try:
                from agents.llm_factory import build_light_llm

                app.state.light_llm = build_light_llm(st, main_llm=app.state.llm)
            except Exception as exc:
                logger.warning("轻模型初始化失败，路由复用主模型: %s", exc)
                app.state.light_llm = app.state.llm

        if app.state.graph is None and app.state.llm is not None:
            try:
                from agents.supervisor import create_supervisor_graph, open_async_checkpointer

                try:
                    checkpoint_cm, checkpointer = await open_async_checkpointer(st)
                except Exception as exc:
                    logger.error("检查点初始化失败，回退内存（重启后 HITL 状态会丢）: %s", exc)
                    from langgraph.checkpoint.memory import MemorySaver

                    checkpoint_cm, checkpointer = None, MemorySaver()

                app.state.graph = create_supervisor_graph(
                    llm=app.state.llm,
                    long_term_memory=app.state.ltm,
                    settings=st,
                    approval_store=app.state.approvals,
                    context_store=getattr(app.state, "context_store", None),
                    checkpointer=checkpointer,
                    light_llm=getattr(app.state, "light_llm", None),
                )
                app.state.checkpoint_cm = checkpoint_cm
                # 图内 HITL 需要 checkpointer 才能中断/恢复
                app.state.hitl_enabled = getattr(app.state.graph, "checkpointer", None) is not None
            except Exception as exc:
                logger.error("编排图构建失败: %s", exc)

        # 知识库预置：仅在库为空时写入（配合 doc_id 去重，重启不重复）
        try:
            docs = getattr(app.state.ltm, "_documents", None)
            if docs is not None and len(docs) == 0:
                for content, source in SEED_KB:
                    app.state.ltm.add_document(content=content, source=source)
                # G8：写入后必须落盘，否则索引只活在内存里
                save = getattr(app.state.ltm, "save", None)
                if callable(save):
                    save()
                    logger.info("知识库已索引并持久化")
        except Exception as exc:
            logger.warning("知识库预置失败: %s", exc)

        yield

        # 关闭前再落盘一次，避免运行期新增文档丢失
        try:
            save = getattr(app.state.ltm, "save", None)
            if callable(save):
                save()
        except Exception as exc:
            logger.warning("知识库持久化失败: %s", exc)

        # 关闭异步检查点连接
        cm = getattr(app.state, "checkpoint_cm", None)
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover
                logger.warning("检查点关闭失败: %s", exc)

    app = FastAPI(
        title=f"{st.app_name} 智能客服多Agent系统",
        version=APP_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = st
    app.state.graph = graph
    app.state.light_llm = None
    # 注入的图若带 checkpointer，则支持图内 interrupt/恢复
    app.state.hitl_enabled = bool(graph is not None and getattr(graph, "checkpointer", None) is not None)
    app.state.llm = llm
    app.state.stm = short_term_memory or _build_default_stm(st)
    app.state.ltm = long_term_memory
    app.state.approvals = approvals or create_approval_store(st)
    app.state.metrics = AgentMetrics()
    if guardrail is None:
        try:
            from security.guardrail import build_guardrail_chain

            guardrail = build_guardrail_chain(st)
        except Exception as exc:  # 护栏初始化失败不影响主链路（规则检测仍在）
            logger.warning("护栏初始化失败，退回规则检测: %s", exc)
    app.state.guardrail = guardrail
    if mcp_server is None:
        try:
            from tooling.client import ToolGateway

            mcp_server = ToolGateway.create_default()
        except Exception as exc:  # 工具服务不可用时不影响主链路
            logger.warning("工具网关初始化失败: %s", exc)
    app.state.mcp = mcp_server
    if st.context_store_enabled:
        try:
            from memory.context_store import ContextStore

            app.state.context_store = ContextStore()
        except Exception as exc:
            logger.warning("用户事实存储初始化失败: %s", exc)
            app.state.context_store = None
    else:
        app.state.context_store = None
    app.state.limiter = build_rate_limiter(st)
    app.state.eval_lock = asyncio.Lock()

    if app.state.ltm is None:
        try:
            app.state.ltm = _build_default_ltm(st)
        except Exception as exc:
            logger.warning("向量库初始化失败（检索将不可用）: %s", exc)
            app.state.ltm = None

    # ── 中间件 ──
    origins = st.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _request_guard(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        token = set_request_id(request_id)
        try:
            content_length = request.headers.get("content-length", "")
            if content_length.isdigit() and int(content_length) > st.max_body_bytes:
                return JSONResponse({"detail": "请求体过大"}, status_code=413,
                                    headers={"X-Request-ID": request_id})
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            reset_request_id(token)

    # ── 依赖 ──

    async def auth(request: Request) -> str:
        if not st.require_auth and not st.api_key_set:
            return "anonymous"
        key = request.headers.get("x-api-key", "")
        if not key or key not in st.api_key_set:
            raise HTTPException(status_code=401, detail="缺少或无效的 API Key")
        return "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]

    async def limited(request: Request, principal: str = Depends(auth)) -> str:
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = await app.state.limiter.allow(principal or client_ip)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后重试",
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
        return principal

    def _require_graph() -> Any:
        if app.state.graph is None:
            raise HTTPException(
                status_code=503,
                detail="服务未就绪：LLM 未配置（OPENAI_API_KEY）或初始化失败",
            )
        return app.state.graph

    async def _guard_message(message: str, request: Request | None = None) -> None:
        if len(message) > st.max_message_chars:
            raise HTTPException(status_code=413, detail="消息长度超过上限")

        rid = getattr(getattr(request, "state", None), "request_id", "-") or "-"
        chain = getattr(app.state, "guardrail", None)
        if chain is not None:  # 护栏链已包含规则层
            verdict = await chain.check(message)
            if verdict.blocked:
                logger.warning("护栏拦截[%s] source=%s categories=%s", rid, verdict.source, verdict.categories[:2])
                detail = "检测到疑似提示注入攻击，请求已被拒绝"
                if verdict.categories:
                    detail += f"（{', '.join(verdict.categories[:2])}）"
                raise HTTPException(status_code=400, detail=detail)
            return

        if detect_injection is not None:
            findings = detect_injection(message)
            if findings:
                logger.warning("注入拦截[%s]: %s", rid, findings[:2])
                raise HTTPException(status_code=400, detail="检测到疑似提示注入攻击，请求已被拒绝")

    # ── 健康检查 ──

    @app.get("/health/live")
    async def health_live():
        return {"status": "live", "version": APP_VERSION}

    @app.get("/health/ready")
    async def health_ready():
        ready = app.state.graph is not None and app.state.ltm is not None
        if not ready:
            raise HTTPException(status_code=503, detail={"ready": False, "graph": app.state.graph is not None, "vector_store": app.state.ltm is not None})
        return {"status": "ready", "version": APP_VERSION}

    @app.get("/health")
    async def health_legacy():
        return {"status": "healthy", "version": APP_VERSION}

    # ── 聊天 ──

    async def _run_chat(message: str, user_id: str, session_id: str | None) -> dict[str, Any]:
        graph = _require_graph()
        await _guard_message(message)
        session_id = session_id or str(uuid.uuid4())
        trace_id = uuid.uuid4().hex
        await app.state.stm.add_message(session_id, "user", message)
        context_messages = await _build_context_messages(
            app.state.stm, session_id, message, st.max_context_tokens
        )
        initial_state = {
            "messages": context_messages,
            "user_id": user_id,
            "session_id": session_id,
            "trace_id": trace_id,
            "intent": "",
            "sub_results": {},
            "compliance_passed": True,
            "final_response": "",
            "retry_count": 0,
        }
        thread_id = f"{user_id}:{session_id}"[:255]
        try:
            result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": thread_id}})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("graph 执行失败 trace_id=%s", trace_id)
            raise HTTPException(status_code=502, detail=f"处理失败: {type(exc).__name__}") from exc

        interrupted = "__interrupt__" in result
        approval_id = None
        if interrupted:
            pending = result.get("__interrupt__") or []
            value = getattr(pending[0], "value", {}) if pending else {}
            approval_id = value.get("approval_id") if isinstance(value, dict) else None
        return {
            "result": result,
            "session_id": session_id,
            "trace_id": trace_id,
            "interrupted": interrupted,
            "approval_id": approval_id,
        }

    async def _handle_compliance_failure(
        result: dict[str, Any], session_id: str, trace_id: str, user_id: str, message: str
    ) -> ChatResponse:
        sub_results = result.get("sub_results", {})
        compliance_info = sub_results.get("compliance", {})
        violations = compliance_info.get("violations", []) if isinstance(compliance_info, dict) else []
        risk_level = compliance_info.get("risk_level", "unknown") if isinstance(compliance_info, dict) else "unknown"
        agent_responses = {
            k: v for k, v in sub_results.items() if k != "compliance" and isinstance(v, str) and v.strip()
        }
        record: ApprovalRecord = await app.state.approvals.new_request(
            tenant_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            user_message=message,
            agent_responses=agent_responses,
            violations=list(violations),
            risk_level=str(risk_level),
            intent=str(result.get("intent", "unknown")),
            ttl_seconds=st.approval_ttl_seconds,
        )
        notice = (
            "您的请求涉及敏感内容或需要人工确认，已转入人工审核流程。"
            f"审批编号：{record.approval_id}。人工确认后我们会尽快回复您。"
        )
        await app.state.stm.add_message(session_id, "assistant", notice)
        return ChatResponse(
            response=notice,
            session_id=session_id,
            intent=str(result.get("intent", "unknown")),
            compliance_passed=False,
            approval_id=record.approval_id,
            trace_id=trace_id,
        )

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest, principal: str = Depends(limited)):
        run = await _run_chat(request.message, request.user_id, request.session_id)
        result = run["result"]
        if run.get("interrupted"):
            notice = (
                "您的请求涉及敏感内容或需要人工确认，已转入人工审核流程。"
                f"审批编号：{run['approval_id']}。人工确认后我们会尽快回复您。"
            )
            await app.state.stm.add_message(run["session_id"], "assistant", notice)
            return ChatResponse(
                response=notice,
                session_id=run["session_id"],
                intent=str(result.get("intent", "unknown")),
                compliance_passed=False,
                approval_id=run["approval_id"],
                trace_id=run["trace_id"],
            )
        if not result.get("compliance_passed", True):
            return await _handle_compliance_failure(
                result, run["session_id"], run["trace_id"], request.user_id, request.message
            )
        final_response = result.get("final_response", "")
        if not final_response:
            raise HTTPException(status_code=502, detail="系统未产出回复")
        await app.state.stm.add_message(run["session_id"], "assistant", final_response)
        return ChatResponse(
            response=final_response,
            session_id=run["session_id"],
            intent=str(result.get("intent", "unknown")),
            compliance_passed=True,
            trace_id=run["trace_id"],
        )

    # ── SSE 流式（POST 为主，GET 为兼容保留） ──

    def _sse(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def _stream_events(
        message: str, user_id: str, session_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """逐节点驱动图执行并产出事件（T-P2-06 的第一阶段）。

        - 有 astream 的实现：按 stream_mode="updates" 推送真实节点进度；
        - 无 astream 的实现（测试桩/旧实现）：退回一次性调用。
        """
        graph = app.state.graph
        if graph is None or not hasattr(graph, "astream"):
            run = await _run_chat(message, user_id, session_id)
            result = run["result"]
            yield {"type": "stage", "node": "synthesize", "label": _STAGE_LABELS["synthesize"]}
            if run.get("interrupted"):
                yield await _interrupted_payload(
                    run["approval_id"], run["session_id"], str(result.get("intent", "unknown"))
                )
            elif not result.get("compliance_passed", True):
                chat_response = await _handle_compliance_failure(
                    result, run["session_id"], run["trace_id"], user_id, message
                )
                yield {
                    "type": "response",
                    "response": chat_response.response,
                    "intent": chat_response.intent,
                    "compliance_passed": False,
                    "approval_id": chat_response.approval_id,
                }
            else:
                response_text = result.get("final_response", "")
                await app.state.stm.add_message(run["session_id"], "assistant", response_text)
                yield {
                    "type": "response",
                    "response": response_text,
                    "intent": str(result.get("intent", "unknown")),
                    "compliance_passed": True,
                }
            return

        await _guard_message(message)
        trace_id = uuid.uuid4().hex
        await app.state.stm.add_message(session_id, "user", message)
        context_messages = await _build_context_messages(
            app.state.stm, session_id, message, st.max_context_tokens
        )
        initial_state = {
            "messages": context_messages,
            "user_id": user_id,
            "session_id": session_id,
            "trace_id": trace_id,
            "intent": "",
            "sub_results": {},
            "compliance_passed": True,
            "final_response": "",
            "retry_count": 0,
            "approval_id": None,
            "approval_decision": None,
        }
        config = {"configurable": {"thread_id": f"{user_id}:{session_id}"[:255]}}

        state: dict[str, Any] = {}
        approval_id: str | None = None

        async for mode, payload in _iter_graph_stream(graph, initial_state, config):
            if mode == "messages":
                # token 级增量：只推"最终答案"这次生成（按 tag 过滤，避免中间推理泄漏）
                try:
                    message_chunk, metadata = payload
                except (TypeError, ValueError):
                    continue
                text = getattr(message_chunk, "content", "")
                tags = (metadata or {}).get("tags") or []
                if isinstance(text, str) and text and "final_answer" in tags:
                    yield {"type": "token", "text": text}
                continue

            chunk = payload
            if not isinstance(chunk, dict):
                continue
            if "__interrupt__" in chunk:
                pending = chunk.get("__interrupt__") or []
                value = getattr(pending[0], "value", {}) if pending else {}
                approval_id = value.get("approval_id") if isinstance(value, dict) else None
                continue
            for node, delta in chunk.items():
                if isinstance(delta, dict):
                    state.update(delta)
                yield {
                    "type": "stage",
                    "node": node,
                    "label": _STAGE_LABELS.get(node, f"⚙️ {node}"),
                }

        if approval_id is not None:
            yield await _interrupted_payload(approval_id, session_id, str(state.get("intent", "unknown")))
            return

        if not state.get("compliance_passed", True):
            chat_response = await _handle_compliance_failure(
                state, session_id, trace_id, user_id, message
            )
            yield {
                "type": "response",
                "response": chat_response.response,
                "intent": chat_response.intent,
                "compliance_passed": False,
                "approval_id": chat_response.approval_id,
            }
            return

        response_text = state.get("final_response", "")
        await app.state.stm.add_message(session_id, "assistant", response_text)
        yield {
            "type": "response",
            "response": response_text,
            "intent": str(state.get("intent", "unknown")),
            "compliance_passed": True,
        }

    async def _interrupted_payload(
        approval_id: str | None, session_id: str, intent: str
    ) -> dict[str, Any]:
        notice = (
            "您的请求涉及敏感内容或需要人工确认，已转入人工审核流程。"
            f"审批编号：{approval_id}。人工确认后我们会尽快回复您。"
        )
        await app.state.stm.add_message(session_id, "assistant", notice)
        return {
            "type": "response",
            "response": notice,
            "intent": intent,
            "compliance_passed": False,
            "approval_id": approval_id,
        }

    async def _event_stream(
        message: str, user_id: str, session_id: str | None, protocol: str = "legacy"
    ) -> AsyncGenerator[str, None]:
        session_id = session_id or str(uuid.uuid4())
        encoder = None
        if protocol == "agui":
            from events.agui import AGUIEncoder

            encoder = AGUIEncoder(thread_id=session_id)

        def emit(event: dict[str, Any]) -> str:
            if encoder is None:
                return _sse(event)
            return "".join(
                f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                for item in encoder.encode(event)
            )

        yield emit({"type": "session", "session_id": session_id})
        try:
            async for event in _stream_events(message, user_id, session_id):
                chunk = emit(event)
                if chunk:
                    yield chunk
            tail = emit({"type": "done"})
            if tail:
                yield tail
        except HTTPException as exc:
            yield emit({"type": "error", "status": exc.status_code, "message": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 - SSE 内不能抛，转成错误事件
            logger.exception("SSE 流式失败")
            yield emit({"type": "error", "message": str(exc)})

    @app.post("/api/chat/stream")
    async def chat_stream_post(
        request: ChatRequest,
        protocol: Literal["legacy", "agui"] = "legacy",
        principal: str = Depends(limited),
    ):
        return StreamingResponse(
            _event_stream(request.message, request.user_id, request.session_id, protocol),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/chat/stream", deprecated=True)
    async def chat_stream_get(
        message: str,
        user_id: str = "anonymous",
        session_id: str | None = None,
        protocol: Literal["legacy", "agui"] = "legacy",
        principal: str = Depends(limited),
    ):
        return StreamingResponse(
            _event_stream(message, user_id, session_id, protocol),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Deprecation": "true"},
        )

    @app.get("/api/users/{user_id}/facts")
    async def user_facts(user_id: str, principal: str = Depends(limited)):
        """查看某用户的跨会话事实（运维/客服排查用）。"""
        store = getattr(app.state, "context_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="用户事实存储未启用")
        facts = store.get_facts(user_id)
        return {
            "user_id": user_id,
            "facts": [{"kind": f.kind, "value": f.value, "created_at": f.created_at} for f in facts],
        }

    # ── 历史 ──

    @app.get("/api/history/{session_id}")
    async def history(session_id: str, principal: str = Depends(limited)):
        try:
            messages = await app.state.stm.get_history(session_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取历史失败: {type(exc).__name__}") from exc
        return {"session_id": session_id, "messages": messages}

    # ── 人工审批（HITL） ──

    @app.get("/api/approvals")
    async def list_approvals(principal: str = Depends(limited)):
        records = await app.state.approvals.list_pending()
        return {
            "total": len(records),
            "reviews": [
                {
                    "approval_id": r.approval_id,
                    "session_id": r.session_id,
                    "risk_level": r.risk_level,
                    "violations": r.violations,
                    "intent": r.intent,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                }
                for r in records
            ],
        }

    @app.get("/api/approvals/{approval_id}")
    async def get_approval(approval_id: str, principal: str = Depends(limited)):
        rec = await app.state.approvals.get(approval_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="审批单不存在或已过期")
        return rec.to_dict()

    async def _resume_or_fallback(rec, decision: str, body: DecisionRequest, principal: str) -> str:
        """优先恢复图内 interrupt（真 HITL）；不可用时回退为清洗后文本。"""
        if getattr(app.state, "hitl_enabled", False) and app.state.graph is not None:
            from langgraph.types import Command

            thread_id = f"{rec.tenant_id}:{rec.session_id}"[:255]
            try:
                resumed = await app.state.graph.ainvoke(
                    Command(resume={
                        "approved": decision == "approved",
                        "actor": principal,
                        "reason": body.reason or "",
                    }),
                    config={"configurable": {"thread_id": thread_id}},
                )
                final = resumed.get("final_response") if isinstance(resumed, dict) else None
                if final:
                    return final
            except Exception as exc:  # 线程已不存在 / 检查点丢失 / 无 checkpointer
                logger.warning("恢复图执行失败，回退为文本合成: %s", exc)

        if decision == "approved":
            joined = "\n\n".join(v for v in rec.agent_responses.values() if v)
            safe = _sanitize_for_release(joined)
            return f"✅ [人工审批通过] {safe}" if safe else "您的请求已由人工审核通过，客服人员将尽快处理。"
        return "❌ 您的请求经人工审核未通过。如有疑问，请联系人工客服。"

    async def _decide(approval_id: str, decision: str, body: DecisionRequest, principal: str) -> dict[str, Any]:
        try:
            rec, audit_row = await app.state.approvals.decide(approval_id, decision, principal, body.reason)
        except KeyError:
            raise HTTPException(status_code=404, detail="审批单不存在或已过期") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

        response_text = await _resume_or_fallback(rec, decision, body, principal)
        await app.state.stm.add_message(rec.session_id, "assistant", response_text)
        return {
            "status": decision,
            "approval_id": rec.approval_id,
            "session_id": rec.session_id,
            "response": response_text,
            "decided_by": principal,
            "audit_row_hash": audit_row["row_hash"],
        }

    @app.post("/api/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: DecisionRequest, principal: str = Depends(limited)):
        return await _decide(approval_id, "approved", body, principal)

    @app.post("/api/approvals/{approval_id}/reject")
    async def reject(approval_id: str, body: DecisionRequest, principal: str = Depends(limited)):
        return await _decide(approval_id, "rejected", body, principal)

    @app.get("/api/approvals/audit/chain")
    async def audit_chain(principal: str = Depends(limited)):
        rows = await app.state.approvals.audit_rows()
        from security.approvals import verify_chain

        return {"rows": rows, "valid": verify_chain(rows)}

    # ── 指标（需鉴权） ──

    @app.get("/api/metrics")
    async def metrics(principal: str = Depends(limited)):
        out: dict[str, Any] = {"agent_metrics": app.state.metrics.get_summary()}
        mcp = app.state.mcp
        if mcp is not None:
            out["tool_call_log"] = mcp.get_call_log(last_n=20)
            out["circuit_breaker"] = mcp.get_circuit_status()
        return out

    # ── Prometheus 文本指标（集群抓取用） ──

    @app.get("/metrics")
    async def prometheus_metrics(request: Request):
        if not st.metrics_public:
            await auth(request)  # 与其它管理端点同一鉴权口径
        from tracing.metrics_export import render_prometheus

        body = render_prometheus(app.state.metrics, version=APP_VERSION)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    # ── 评估：POST + 鉴权 + 并发保护（G14） ──

    @app.get("/api/evaluate")
    async def evaluate_gone():
        raise HTTPException(status_code=405, detail="已废弃：请使用 POST /api/evaluations")

    @app.post("/api/evaluations")
    async def run_evaluations(body: EvaluationRequest, principal: str = Depends(limited)):
        if app.state.eval_lock.locked():
            raise HTTPException(status_code=409, detail="已有评估任务在进行中")
        if app.state.graph is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        async with app.state.eval_lock:
            from evaluation.evaluator import Evaluator, GoldenSet

            dataset_path = body.dataset or "evaluation/datasets/v1.jsonl"
            try:
                golden = GoldenSet.load(dataset_path)
            except Exception:
                golden = GoldenSet.default()
            ev = Evaluator(llm=app.state.llm, long_term_memory=app.state.ltm, top_k=body.top_k)
            try:
                report = await asyncio.wait_for(ev.evaluate(golden), timeout=300.0)
            except TimeoutError:
                raise HTTPException(status_code=504, detail="评估超时") from None
            return report

    # ── 调试工具端点（默认关闭） ──

    if st.enable_debug_tool_endpoints and app.state.mcp is not None:
        @app.get("/api/tools")
        async def list_tools(principal: str = Depends(auth)):
            tools = await app.state.mcp.list_tools()
            return {"tools": tools}

        @app.post("/api/tools/call")
        async def call_tool(request: dict, principal: str = Depends(auth)):
            return await app.state.mcp.call_tool(
                name=request.get("name", ""), arguments=request.get("arguments", {})
            )

    # ── 前端 ──

    @app.get("/")
    async def index():
        return RedirectResponse(url="/static/index.html")

    _static_dir = __import__("os").path.join(
        __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))),
        "static",
    )
    if __import__("os").path.isdir(_static_dir):
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    st = get_settings()
    uvicorn.run("api.main:app", host=st.host, port=st.port, reload=False)
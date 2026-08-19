"""
FastAPI入口 — 提供REST API + SSE流式响应
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.supervisor import create_supervisor_graph
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from mcp.mcp_server import MCPToolServer, create_default_tools
from tracing.otel_config import init_tracer, AgentMetrics, set_global_metrics

load_dotenv()


# ─── Human-in-the-Loop 审核队列（Q5）───
# 合规审查未通过时，暂停自动回复，等待人工审批
# 结构: { session_id: { user_message, agent_response, violations, intent, timestamp } }
pending_reviews: dict[str, dict] = {}


# ─── Token 预算（Q7）───
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "4000"))


def _estimate_tokens(text: str) -> int:
    """
    简易 Token 估算（无需联网下载编码表）。
    中文约 1 字符 ≈ 0.6 token，英文约 4 字符 ≈ 1 token。
    取保守值：字符数 / 1.8。
    """
    if not text:
        return 0
    return max(1, int(len(text) / 1.8))


async def _build_context_messages(
    stm: ShortTermMemory,
    session_id: str,
    current_message: str,
    max_tokens: int = MAX_CONTEXT_TOKENS,
) -> list:
    """
    从短期记忆构建带 Token 预算的上下文消息列表（Q7）。

    流程：
    1. 获取会话历史（含当前消息）
    2. 转换为 HumanMessage/AIMessage
    3. 从最新消息向前截断，总 Token 不超过 max_tokens
    """
    from langchain_core.messages import AIMessage, HumanMessage

    history = await stm.get_history(session_id)
    if not history:
        return [HumanMessage(content=current_message)]

    # 转换为消息对象
    messages = []
    for entry in history:
        role = entry.get("role", "user")
        content = entry.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))

    # 从最新消息向前截断（简易 Token 估算，不依赖 tiktoken 联网下载）
    total, truncated = 0, []
    for msg in reversed(messages):
        tokens = _estimate_tokens(msg.content)
        if total + tokens > max_tokens:
            break
        total += tokens
        truncated.insert(0, msg)

    # 确保最后一条是用户消息
    if truncated and not isinstance(truncated[-1], HumanMessage):
        truncated.append(HumanMessage(content=current_message))

    return truncated if truncated else [HumanMessage(content=current_message)]


async def _write_conversation_summary(
    ltm: LongTermMemory,
    stm: ShortTermMemory,
    session_id: str,
    user_message: str,
    assistant_response: str,
) -> None:
    """
    对话摘要写回长期记忆（Q23）。
    每轮对话结束后，将关键信息写入知识库，
    使后续对话可以检索到历史交互上下文。
    """
    summary = (
        f"[对话记录] 用户({session_id[:8]}): {user_message[:200]}\n"
        f"客服回复: {assistant_response[:200]}"
    )
    ltm.add_document(
        content=summary,
        source=f"conversation_{session_id[:8]}",
        metadata={"type": "conversation_summary", "session_id": session_id},
    )


working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
long_term_memory = LongTermMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
mcp_server = create_default_tools(MCPToolServer())
metrics = AgentMetrics()
graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global graph

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )

    set_global_metrics(metrics)

    graph = create_supervisor_graph(
        working_memory=working_memory,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
    )

    long_term_memory.add_document(
        content="我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。",
        source="product_faq.md",
    )
    long_term_memory.add_document(
        content="退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。",
        source="refund_policy.md",
    )
    long_term_memory.add_document(
        content="开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。",
        source="account_guide.md",
    )

    yield


app = FastAPI(
    title="智能客服多Agent系统",
    description="基于LangGraph的Supervisor编排多Agent智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    主聊天接口。
    合规审查未通过时，自动进入 HITL 人工审批队列（Q5），
    等待 /api/review 端点审批后才返回最终回复。
    """
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())

    await short_term_memory.add_message(session_id, "user", request.message)

    # ── Token 预算：从短期记忆构建截断后的上下文消息（Q7）──
    context_messages = await _build_context_messages(
        short_term_memory, session_id, request.message, MAX_CONTEXT_TOKENS
    )

    initial_state = {
        "messages": context_messages,
        "user_id": request.user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
    }

    config = {"configurable": {"thread_id": session_id}}

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")

    compliance_passed = result.get("compliance_passed", True)

    # ── HITL: 合规未通过 → 加入人工审批队列 ──
    if not compliance_passed:
        # 提取各 Agent 的原始回复（合规审查前）
        sub_results = result.get("sub_results", {})
        compliance_info = sub_results.get("compliance", {})
        violations = compliance_info.get("violations", []) if isinstance(compliance_info, dict) else []

        # 提取 Agent 原始回复内容
        agent_responses = {k: v for k, v in sub_results.items() if k != "compliance" and isinstance(v, str)}

        pending_reviews[session_id] = {
            "user_message": request.message,
            "agent_responses": agent_responses,
            "violations": violations,
            "risk_level": compliance_info.get("risk_level", "unknown") if isinstance(compliance_info, dict) else "unknown",
            "intent": result.get("intent", "unknown"),
            "user_id": request.user_id,
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }

        review_response = (
            "🛡️ 您的请求已触发合规审查，已转交人工客服审批。\n"
            f"审批队列编号: {session_id[:8]}\n"
            "请稍候，人工客服将尽快处理。"
        )
        await short_term_memory.add_message(session_id, "assistant", review_response)

        return ChatResponse(
            response=review_response,
            session_id=session_id,
            intent=result.get("intent", "unknown"),
            compliance_passed=False,
        )

    # ── 正常流程 ──
    final_response = result.get("final_response", "系统处理异常，请稍后重试")

    await short_term_memory.add_message(session_id, "assistant", final_response)

    # ── 对话摘要写回长期记忆（Q23）──
    await _write_conversation_summary(
        long_term_memory, short_term_memory, session_id,
        request.message, final_response,
    )

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=result.get("intent", "unknown"),
        compliance_passed=True,
    )


# ─── SSE 流式聊天接口 ───

_STAGE_LABELS = {
    "supervisor_route":  "🧠 正在分析您的意图…",
    "knowledge_rag":     "📚 正在检索知识库…",
    "ticket_handler":    "🎫 正在处理工单…",
    "compliance_check":  "🛡️ 正在进行合规审查…",
    "synthesize":        "✍️ 正在生成回复…",
}


@app.get("/api/chat/stream")
async def chat_stream(message: str, user_id: str = "anonymous", session_id: str | None = None):
    """SSE 流式聊天 — 逐阶段推送处理进度和最终回复"""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = session_id or str(uuid.uuid4())

    await short_term_memory.add_message(session_id, "user", message)

    from langchain_core.messages import HumanMessage

    initial_state = {
        "messages": [HumanMessage(content=message)],
        "user_id": user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
    }

    config = {"configurable": {"thread_id": session_id}}

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # 先发 session 信息
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            async for event in graph.astream(initial_state, config=config, stream_mode="updates"):
                for node_name, node_output in event.items():
                    label = _STAGE_LABELS.get(node_name, f"⚙️ {node_name}…")
                    yield f"data: {json.dumps({'type': 'stage', 'node': node_name, 'label': label}, ensure_ascii=False)}\n\n"

                    # 如果是最终节点，提取回复
                    if node_name == "synthesize" and isinstance(node_output, dict):
                        final = node_output.get("final_response", "")
                        intent = node_output.get("intent", "unknown")
                        passed = node_output.get("compliance_passed", True)
                        if final:
                            yield f"data: {json.dumps({'type': 'response', 'response': final, 'intent': intent, 'compliance_passed': passed}, ensure_ascii=False)}\n\n"
                            await short_term_memory.add_message(session_id, "assistant", final)

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    """获取对话历史"""
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.get("/api/tools")
async def list_tools():
    """MCP工具发现接口"""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: dict):
    """MCP工具调用接口"""
    result = await mcp_server.call_tool(
        name=request.get("name", ""),
        arguments=request.get("arguments", {}),
    )
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/metrics")
async def get_metrics():
    """获取系统指标"""
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
        "circuit_breaker": mcp_server.get_circuit_status(),
    }


@app.get("/api/evaluate")
async def run_evaluation():
    """Golden Set 自动评估（Q30）— Recall@K / MRR / Faithfulness"""
    from evaluation.evaluator import Evaluator, GoldenSet
    from langchain_openai import ChatOpenAI

    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    ev = Evaluator(
        llm=ChatOpenAI(model=os.getenv("MODEL_NAME", "gpt-4o"), temperature=0),
        long_term_memory=long_term_memory,
    )
    report = await ev.evaluate(GoldenSet.default())
    return report


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


# ─── HITL 人工审批端点（Q5）───

@app.get("/api/review/pending")
async def list_pending_reviews():
    """获取所有待审批的请求"""
    return {
        "total": len(pending_reviews),
        "reviews": [
            {
                "session_id": sid,
                "user_message": info["user_message"],
                "violations": info["violations"],
                "risk_level": info["risk_level"],
                "intent": info["intent"],
                "timestamp": info["timestamp"],
            }
            for sid, info in pending_reviews.items()
        ],
    }


@app.get("/api/review/{session_id}")
async def get_review(session_id: str):
    """获取某个待审批请求的详情"""
    if session_id not in pending_reviews:
        raise HTTPException(status_code=404, detail="该审批请求不存在或已处理")
    return pending_reviews[session_id]


@app.post("/api/review/{session_id}/approve")
async def approve_review(session_id: str):
    """
    人工审批通过 — 将 Agent 原始回复返回给用户。
    用法: POST /api/review/{session_id}/approve
    可选 body: {"note": "审批备注"}
    """
    if session_id not in pending_reviews:
        raise HTTPException(status_code=404, detail="该审批请求不存在或已处理")

    review = pending_reviews.pop(session_id)

    # 合并各 Agent 的原始回复
    agent_parts = [v for v in review["agent_responses"].values() if v]
    approved_response = "\n\n".join(agent_parts) if agent_parts else "人工审批通过，但原始回复不可用。"

    # 添加审批标注
    approved_response = f"✅ [人工审批通过] {approved_response}"

    await short_term_memory.add_message(session_id, "assistant", approved_response)

    return {
        "status": "approved",
        "session_id": session_id,
        "response": approved_response,
        "original_violations": review["violations"],
    }


@app.post("/api/review/{session_id}/reject")
async def reject_review(session_id: str):
    """
    人工审批拒绝 — 返回拒绝消息，不暴露 Agent 原始内容。
    用法: POST /api/review/{session_id}/reject
    可选 body: {"reason": "拒绝原因"}
    """
    if session_id not in pending_reviews:
        raise HTTPException(status_code=404, detail="该审批请求不存在或已处理")

    review = pending_reviews.pop(session_id)

    rejected_response = (
        "❌ 很抱歉，您的请求经人工审核后未能通过。\n"
        f"原因: 内容涉及敏感信息（{', '.join(review['violations'][:3])}）。\n"
        "如有疑问，请联系人工客服。"
    )

    await short_term_memory.add_message(session_id, "assistant", rejected_response)

    return {
        "status": "rejected",
        "session_id": session_id,
        "response": rejected_response,
        "violations": review["violations"],
    }


# ── 前端页面 ──
@app.get("/")
async def index():
    """跳转到前端聊天页面"""
    return RedirectResponse(url="/static/index.html")


# 挂载静态文件目录（项目根目录下的 static/）
_static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )

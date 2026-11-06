"""离线演示模式（DEMO_MODE=true）。

目的：没有 OPENAI_API_KEY 时也能把 **路由 → 检索 → 合规 → 聚合 → 审批**
整条链路跑起来，用于本地演示与 UI 联调。

⚠ 这不是生产降级：演示模式必须显式开启（默认 false），
且用的是「脚本化假模型 + 关键字检索」，不参与任何真实回答。
生产环境误开会在启动日志里明确警告。
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_TICKET_HINTS = ("退款", "理赔", "开户", "投诉", "销户", "办理", "申请", "工单", "重置密码")
_FORBIDDEN = ("保证收益", "稳赚不赔", "零风险", "保本保息")

DEMO_KB: list[dict[str, str]] = [
    {"id": "kb-1", "source": "product_faq.md",
     "content": "我们的理财产品A年化收益率为3.5%-5.2%，投资期限6个月至3年，最低投资金额10000元。理财非存款，产品有风险，投资须谨慎。"},
    {"id": "kb-2", "source": "refund_policy.md",
     "content": "退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因，退款将在3-5个工作日内原路退回。"},
    {"id": "kb-3", "source": "account_guide.md",
     "content": "开户流程：准备身份证原件、填写申请表、视频认证、设置交易密码、完成风险评估问卷，全程约15-30分钟。"},
]


class ScriptedChatModel(BaseChatModel):
    """按提示词类型返回脚本化回答的假模型（演示用）。"""

    mode: str = "demo"

    @property
    def _llm_type(self) -> str:
        return "demo-scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        # 演示模式不启用工具调用，让业务 Agent 走确定性的文本路径
        raise NotImplementedError("demo mode does not support tool calling")

    # ── 内部：根据系统提示词类型分派 ──

    @staticmethod
    def _last_text(messages: list[BaseMessage]) -> str:
        return "\n".join(str(getattr(m, "content", "")) for m in messages)

    def _reply(self, messages: list[BaseMessage]) -> str:
        # 关键：系统提示词里本身就写着"保证收益"之类的示例词，
        # 因此内容判断只能看非 system 的消息，否则会把自己的提示词当成违规内容。
        system = " ".join(
            str(getattr(m, "content", "")) for m in messages if m.__class__.__name__ == "SystemMessage"
        )
        human = " ".join(
            str(getattr(m, "content", "")) for m in messages if m.__class__.__name__ != "SystemMessage"
        )
        text = human

        if "Supervisor" in system:
            return "ticket_handler" if any(h in human for h in _TICKET_HINTS) else "knowledge_rag"

        if "合规审查" in system:
            hit = next((term for term in _FORBIDDEN if term in human), None)
            if hit:
                return '{"passed": false, "risk_level": "high", "violations": ["包含违规金融用语: ' + hit + '"], "suggestions": ["移除承诺性表述"]}'
            return '{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}'

        if "工单" in system and "JSON" in system.upper():
            kind = "refund" if "退款" in text else "general"
            return ('{"action": "create", "ticket_type": "' + kind + '", "priority": "medium",'
                    ' "summary": "演示工单", "details": "由演示模式生成"}')

        if "改写" in system or "检索的查询" in text:
            return "理财产品A 收益率 退款政策 开户流程"

        if "审查员" in system:
            return "PASS"

        if "知识库问答" in system:
            # 用户索要"保证收益"时，故意给出违规回答，用于演示合规拦截与人工审批
            if any(k in human for k in ("保证", "稳赚", "零风险")):
                return "我们保证收益，零风险，稳赚不赔，保本保息。"
            if "收益" in text or "理财" in text:
                return "理财产品A的年化收益率为 3.5%-5.2%，期限 6 个月至 3 年，最低 10000 元起投。\n\n以上信息仅供参考，具体以合同条款为准。\n\n来源：product_faq.md"
            if "退款" in text:
                return "购买后 7 天内可申请无理由退款，超过 7 天需说明原因，退款 3-5 个工作日原路退回。\n\n来源：refund_policy.md"
            if "开户" in text:
                return "开户需准备身份证原件并完成视频认证与风险测评，全程约 15-30 分钟。\n\n来源：account_guide.md"
            return "抱歉，知识库中暂未找到与您问题高度相关的信息。已为您转接人工客服。"

        return "（演示模式）已收到您的问题。"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._reply(messages)))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class DemoKnowledgeBase:
    """演示用知识库：关键字重叠打分，score 归一化到 [0,1]。

    刻意不使用向量：演示模式不假装拥有 embedding 能力。
    """

    def __init__(self, docs: list[dict[str, str]] | None = None) -> None:
        self._docs = [dict(d) for d in (docs or DEMO_KB)]
        self._documents = self._docs  # 兼容 api 启动时的"是否为空"判断

    @staticmethod
    def _score(query: str, content: str) -> float:
        terms = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]+", query)]
        if not terms:
            return 0.0
        hits = sum(1 for term in terms if term in content)
        return round(hits / len(terms), 4)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        scored = []
        for doc in self._docs:
            score = self._score(query, doc["content"])
            if score > 0:
                scored.append({**doc, "score": score})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]

    async def asearch(self, query: str, top_k: int = 5) -> list[dict]:
        return self.search(query, top_k)

    def add_document(self, content: str, source: str = "", metadata: dict | None = None,
                     context: str | None = None) -> str:
        doc = {"id": f"demo-{len(self._docs) + 1}", "source": source, "content": content}
        self._docs.append(doc)
        return doc["id"]

    def save(self) -> None:  # 演示模式不落盘
        return None


__all__ = ["ScriptedChatModel", "DemoKnowledgeBase", "DEMO_KB"]

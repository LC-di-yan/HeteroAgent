"""寒暄与澄清快路径。

背景：问候/感谢/"你能做什么"这类输入不应进入知识检索——旧行为会走 RAG、检索不到，
再回一句"知识库暂未找到…转接人工"，答非所问。这里用确定性规则直接给出有人味的回复，
并给用户可用的问法建议。

设计取舍：
- 规则命中时**不调用任何模型**，保证亚秒级响应；
- 回复是固定模板（没有模型产出），因此只做单元测试层面的合规自检，不进入 LLM 深审；
- 业务相关但语义模糊的输入仍交给 Supervisor 的模型路由，路由结果为 unknown 时落到
  KIND_CLARIFY（澄清而不是瞎查）。
"""
from __future__ import annotations

import re
from typing import Final

KIND_GREETING: Final = "greeting"
KIND_THANKS: Final = "thanks"
KIND_CAPABILITY: Final = "capability"
KIND_CLARIFY: Final = "clarify"

_GREETING_RE = re.compile(
    r"^(你好|您好|hi|hello|哈喽|哈啰|嗨|在吗|在么|早上好|上午好|下午好|晚上好|早安|晚安)"
    r"[\s!！。.~～,，?？]*$",
    re.IGNORECASE,
)
_THANKS_RE = re.compile(
    r"^(谢谢|感谢|多谢|辛苦了?|thx|thanks?)[\s!！。.~～,，?？]*$",
    re.IGNORECASE,
)
_CAPABILITY_RE = re.compile(
    r"你是谁|你叫什么|你能做什么|能做什么|有什么功能|怎么用|使用说明|帮助|help",
    re.IGNORECASE,
)
# 过短且不含业务线索的输入（"嗯"、"在?"）直接澄清，避免误检索。
# 中文短句很常见（"我要申请退款"只有 6 个字），所以不能只看长度，必须同时没有业务词。
_SHORT_LIMIT = 4
_BUSINESS_HINTS = re.compile(
    r"退款|退货|售后|订单|物流|快递|工单|开户|注销|密码|限额|收益|理财|利率|产品|基金|"
    r"保险|投诉|发票|充值|提现|转账|账单|逾期|审批|合规|风控|查询|办理",
)


def classify_smalltalk(text: str) -> str | None:
    """返回 smalltalk 类型；None 表示应当继续走业务路由。"""
    raw = (text or "").strip()
    if not raw:
        return KIND_CLARIFY
    if _GREETING_RE.match(raw):
        return KIND_GREETING
    if _THANKS_RE.match(raw):
        return KIND_THANKS
    if _CAPABILITY_RE.search(raw):
        return KIND_CAPABILITY
    if (
        len(raw) <= _SHORT_LIMIT
        and not re.search(r"\d", raw)
        and not _BUSINESS_HINTS.search(raw)
    ):
        return KIND_CLARIFY
    return None


_REPLIES: dict[str, str] = {
    KIND_GREETING: (
        "你好，我是 HeteroAgent 客服助手。\n\n"
        "你可以直接说需求，我来判断该查知识库还是建工单：\n\n"
        "- 理财产品 A 的收益率是多少？\n"
        "- 我要申请退款 / 查一下订单状态\n"
        "- 开户需要准备哪些材料？\n\n"
        "也可以直接描述遇到的问题，我会先确认意图再处理。"
    ),
    KIND_THANKS: "不客气，还有问题随时说。",
    KIND_CAPABILITY: (
        "我是这套系统的编排助手，负责把问题分给合适的同事：\n\n"
        "- 知识问答：产品、政策、流程说明\n"
        "- 工单办理：退款、开户、投诉、订单查询\n"
        "- 合规审查：命中风险的回复会转人工审批\n\n"
        "直接说一句需求就行，比如「退款政策是什么」或「我要申请退款」。"
    ),
    KIND_CLARIFY: (
        "我还没完全理解你的意思。可以试试这样问：\n\n"
        "- 「理财产品 A 的收益率是多少？」\n"
        "- 「我要申请退款」\n"
        "- 「帮我查一下订单状态」\n\n"
        "如果是要查具体单据，请带上订单号或工单号；也可以直接描述你遇到的情况。"
    ),
}


def build_reply(kind: str) -> str:
    """按 smalltalk 类型取回复模板；未知类型退化为澄清。"""
    return _REPLIES.get(kind, _REPLIES[KIND_CLARIFY])


__all__ = [
    "KIND_CAPABILITY",
    "KIND_CLARIFY",
    "KIND_GREETING",
    "KIND_THANKS",
    "build_reply",
    "classify_smalltalk",
]

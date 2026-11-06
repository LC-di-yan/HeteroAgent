"""合规审查 Agent —— fail-closed 版（T-P0-02 / G2 / G9-PII）。

相对原实现的变化：
- llm_check 解析失败 → passed=False（fail-closed，原为 fail-open）；
- process() 合规失败时对每个 sub_results 键分别脱敏（原实现整体覆盖为同一段文本）；
- PII 检测升级：手机号边界、身份证 GB11643 校验位、银行卡 Luhn 校验——
  订单号/时间戳等长数字不再误判为银行卡；
- 否定词窗口可配置（settings.compliance_negation_window）且不跨分句边界；
- 输入注入检测已移至 security/injection.py（API 入口前置调用）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from config import Settings
from config import settings as default_settings
from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)

FENCE = chr(96) * 3


@dataclass
class ComplianceResult:
    """合规审查结果。"""

    passed: bool
    risk_level: str  # low / medium / high / critical
    violations: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    sanitized_content: str = ""


# ─── PII 模式（带边界） ───

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = {0: "1", 1: "0", 2: "X", 3: "9", 4: "8", 5: "7", 6: "6", 7: "5", 8: "4", 9: "3", 10: "2"}

FORBIDDEN_TERMS = [
    "保证收益", "稳赚不赔", "零风险", "保本保息",
    "最高收益", "承诺回报", "内部消息", "内幕", "暗箱操作",
]

_NEGATION_PREFIX = re.compile(r"(不|不能|无法|不会|并非|禁止|切勿|请勿|没有|不可|避免|不代表|不等于)")
_SENTENCE_BOUNDARY = "。！？；!?;,.，\n"

_RISK_PRIORITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}

COMPLIANCE_SYSTEM_PROMPT = """你是一个金融合规审查 Agent，负责审查客服回复内容的合规性。

审查维度：
1. 是否包含违规金融用语（如"保证收益"、"零风险"等）
2. 是否泄露用户 PII 信息（手机号、身份证号、银行卡号）
3. 是否存在越权承诺（如擅自承诺退款/赔偿金额）
4. 是否符合金融监管要求（风险提示、免责声明）
5. 是否包含歧视性、侮辱性内容

只返回 JSON，格式：
{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}
"""


def _luhn_valid(digits: str) -> bool:
    """标准 Luhn 校验。"""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _id_card_valid(digits17: str, check: str) -> bool:
    """GB 11643 mod 11-2 校验位验证。"""
    try:
        s = sum(int(digits17[i]) * _ID_WEIGHTS[i] for i in range(17))
    except (ValueError, IndexError):
        return False
    return _ID_CHECK.get(s % 11, "!") == check.upper()


def mask_pii(content: str) -> str:
    """按类型策略脱敏：手机 138****1234；身份证 前3***后4；银行卡 前4****后4；邮箱 a***@domain。"""

    def mask_phone(m: re.Match) -> str:
        t = m.group()
        return t[:3] + "****" + t[-4:]

    def mask_id(m: re.Match) -> str:
        t = m.group()
        return t[:3] + "***" + t[-4:]

    def mask_bank(m: re.Match) -> str:
        t = m.group()
        return t[:4] + "****" + t[-4:]

    def mask_email(m: re.Match) -> str:
        local, _, domain = m.group().partition("@")
        head = local[:1] if local else "*"
        return head + "***@" + domain

    masked = _EMAIL_RE.sub(mask_email, content or "")
    masked = _PHONE_RE.sub(mask_phone, masked)
    masked = _BANK_CARD_RE.sub(lambda m: mask_bank(m) if _luhn_valid(m.group()) else m.group(), masked)
    masked = _ID_CARD_RE.sub(
        lambda m: mask_id(m) if _id_card_valid(m.group()[:17], m.group()[-1]) else m.group(), masked
    )
    return masked


def sanitize_output(text: str) -> str:
    """对外发布前清洗：PII 脱敏 + 违规金融用语替换（G3 的最后防线）。"""
    masked = mask_pii(text or "")
    for term in FORBIDDEN_TERMS:
        masked = masked.replace(term, "[已屏蔽]")
    return masked


class ComplianceCheckerAgent:
    """两阶段合规审查：规则引擎（快）→ LLM 深审（语义），全程 fail-closed。"""

    def __init__(self, llm: Any, settings: Settings = default_settings):
        self.llm = llm
        self.settings = settings

    # ── 规则引擎 ──

    def _negation_prefix_hit(self, content: str, idx: int) -> bool:
        """违规词前窗口内是否有否定词；窗口不跨越分句边界。"""
        window = int(getattr(self.settings, "compliance_negation_window", 8))
        start = max(0, idx - window)
        prefix = content[start:idx]
        boundary = max(prefix.rfind(ch) for ch in _SENTENCE_BOUNDARY)
        if boundary != -1:
            prefix = prefix[boundary + 1:]
        return bool(_NEGATION_PREFIX.search(prefix))

    def _rule_based_check(self, content: str) -> list[str]:
        violations: list[str] = []
        for term in FORBIDDEN_TERMS:
            start = 0
            while True:
                idx = content.find(term, start)
                if idx == -1:
                    break
                if not self._negation_prefix_hit(content, idx):
                    violations.append(f"包含违规金融用语: '{term}'")
                    break
                start = idx + len(term)
        if _PHONE_RE.search(content):
            violations.append("检测到PII信息泄露: 手机号")
        if any(_id_card_valid(m.group()[:17], m.group()[-1]) for m in _ID_CARD_RE.finditer(content)):
            violations.append("检测到PII信息泄露: 身份证号")
        if any(_luhn_valid(m.group()) for m in _BANK_CARD_RE.finditer(content)):
            violations.append("检测到PII信息泄露: 银行卡号")
        if _EMAIL_RE.search(content):
            violations.append("检测到PII信息泄露: 邮箱地址")
        return violations

    @trace_agent_call("compliance_rule_check")
    async def rule_check(self, content: str) -> ComplianceResult:
        violations = self._rule_based_check(content or "")
        sanitized = mask_pii(content or "")
        if not violations:
            return ComplianceResult(passed=True, risk_level="low", sanitized_content=sanitized)

        has_pii = any("PII" in v for v in violations)
        has_forbidden = any("违规金融用语" in v for v in violations)
        if has_pii and has_forbidden:
            risk_level = "critical"
        elif has_pii or has_forbidden:
            risk_level = "high"
        else:
            risk_level = "medium"
        return ComplianceResult(passed=False, risk_level=risk_level, violations=violations,
                                sanitized_content=sanitized)

    # ── LLM 深审 ──

    @staticmethod
    def _strip_fences(text: str) -> str:
        text = (text or "").strip()
        if text.startswith(FENCE):
            text = text[len(FENCE):]
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.rstrip()
            if text.endswith(FENCE):
                text = text[: -len(FENCE)]
        return text.strip()

    @trace_agent_call("compliance_llm_check")
    async def llm_check(self, content: str) -> ComplianceResult:
        sanitized = mask_pii(content or "")
        messages = [
            SystemMessage(content=COMPLIANCE_SYSTEM_PROMPT),
            HumanMessage(content=f"请审查以下客服回复内容的合规性：\n\n{content}"),
        ]
        try:
            response = await self.llm.ainvoke(messages)
        except Exception as exc:
            logger.warning("合规 LLM 审查调用失败，fail-closed: %s", type(exc).__name__)
            return ComplianceResult(passed=False, risk_level="high",
                                    violations=[f"合规审查服务异常: {type(exc).__name__}"],
                                    suggestions=["人工复核"], sanitized_content=sanitized)

        raw = self._strip_fences(getattr(response, "content", "") or "")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("not a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("合规 LLM 结果无法解析（%s），fail-closed", exc)
            return ComplianceResult(passed=False, risk_level="high",
                                    violations=["合规审查结果无法解析"],
                                    suggestions=["人工复核"], sanitized_content=sanitized)

        risk_level = result.get("risk_level", "low")
        if risk_level not in _RISK_PRIORITY:
            risk_level = "medium"
        return ComplianceResult(
            passed=bool(result.get("passed", False)),
            risk_level=risk_level,
            violations=list(result.get("violations", []) or []),
            suggestions=list(result.get("suggestions", []) or []),
            sanitized_content=sanitized,
        )

    @trace_agent_call("compliance_full_check")
    async def full_check(self, content: str) -> ComplianceResult:
        rule_result = await self.rule_check(content)
        if not rule_result.passed and rule_result.risk_level in ("high", "critical"):
            return rule_result

        llm_result = await self.llm_check(content)
        all_violations = rule_result.violations + llm_result.violations
        final_passed = rule_result.passed and llm_result.passed
        final_risk = max(rule_result.risk_level, llm_result.risk_level,
                         key=lambda r: _RISK_PRIORITY.get(r, 0))
        return ComplianceResult(passed=final_passed, risk_level=final_risk,
                                violations=all_violations,
                                suggestions=llm_result.suggestions,
                                sanitized_content=rule_result.sanitized_content)

    # ── 图节点入口 ──

    @trace_agent_call("compliance_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """输出审查。G2 修复：对每个子结果分别脱敏，绝不整体覆盖。"""
        sub_results = state.get("sub_results", {})

        content_to_check = "\n".join(
            value for key, value in sub_results.items()
            if key != "compliance" and isinstance(value, str) and value.strip()
        )

        sanitized_results = {
            key: mask_pii(value) if isinstance(value, str) else value
            for key, value in sub_results.items()
        }

        if not content_to_check.strip():
            return {**state, "compliance_passed": True, "sub_results": sanitized_results}

        compliance_result = await self.full_check(content_to_check)

        return {
            **state,
            "compliance_passed": compliance_result.passed,
            "sub_results": {
                **sanitized_results,
                "compliance": {
                    "passed": compliance_result.passed,
                    "risk_level": compliance_result.risk_level,
                    "violations": compliance_result.violations,
                },
            },
        }

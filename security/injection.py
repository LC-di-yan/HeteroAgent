"""提示注入检测（T-P0-01 / G1 修复的前置件）。

在 API 入口调用（写 Redis、构图、任何 LLM 调用之前）；
合规节点不再承担输入检测职责。

对抗绕过对策：NFKC 归一化（全角→半角）、去零宽字符、
统一小写、压缩空白后再匹配；模式本身对词间空白容忍。
"""
from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)

# (编译后的模式, 违规描述)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ── 角色劫持 / 指令覆盖 ──
    (re.compile(r"忽略.{0,12}(以上|之前|前面|上面|先前).{0,12}(指令|规则|提示|要求|设定|约束)"), "角色劫持：试图让模型忽略既有指令"),
    (re.compile(r"ignore(\s|\w)*all(\s|\w)*previous(\s|\w)*(instructions?|rules?|prompts?|constraints?)", re.I), "指令覆盖：ignore all previous instructions"),
    (re.compile(r"disregard(\s|\w)*(all\s)?(previous|prior|above|earlier)(\s|\w)*(instructions?|rules?|prompts?|guidance)", re.I), "指令覆盖：disregard previous instructions"),
    (re.compile(r"(你|你现在)(是|将会是|变成).{0,24}(黑客|攻击者|越狱|无限制|不受限|jailbreak|cracker)"), "角色劫持：试图改写模型身份"),
    (re.compile(r"从(现在|此刻)起.{0,16}(你|你的).{0,16}(角色|身份|任务|目标|设定)"), "角色劫持：从现在起改变角色"),
    (re.compile(r"do\s*anything\s*now|\bDAN\b(\s*(模式|mode|jailbreak))?", re.I), "越狱模式：DAN / Do Anything Now"),
    (re.compile(r"越狱|jailbreak", re.I), "越狱关键词"),
    # ── 系统提示词泄露 ──
    (re.compile(r"(显示|输出|打印|告诉我|复述|重复).{0,12}(你的?|系统|system).{0,12}(系统提示|提示词|指令|规则|prompt|instruction)", re.I), "系统提示泄露诱导"),
    (re.compile(r"(reveal|print|show|repeat|output)(\s|\w)*your(\s|\w)*(system|initial|secret)(\s|\w)*(prompt|message|instruction|configuration)", re.I), "系统提示泄露诱导（英文）"),
    (re.compile(r"(你的|您的)系统提示词|系统提示词是什么|system\s*prompt", re.I), "系统提示词刺探"),
    # ── 代码执行诱导 ──
    (re.compile(r"(执行|运行|帮我跑|run|execute|eval).{0,12}(这段|以下|如下)?(代码|命令|脚本|shell|bash|powershell|cmd|python)", re.I), "代码执行诱导"),
    (re.compile(r"os\.system|subprocess|__import__|popen|eval\s*\(|exec\s*\(", re.I), "危险函数调用字样"),
    # ── 编码 / 隐蔽载荷 ──
    (re.compile(r"base64.{0,16}(解码|decode|解出).{0,24}(并|然后|再)?(执行|运行|eval|exec)", re.I), "编码载荷执行诱导"),
    (re.compile(r"(十六进制|hex|unicode|\\u00|\\x[0-9a-f]{2}).{0,24}(解码|还原).{0,24}(执行|运行)", re.I), "编码载荷执行诱导"),
    # ── 工具滥用诱导 ──
    (re.compile(r"(调用|使用).{0,12}(你的?|内部|所有)(工具|函数|接口).{0,20}(删除|转帐|转账|支付|放款|提现)", re.I), "危险工具调用诱导"),
    (re.compile(r"(删除|清空|drop).{0,12}(所有|全部|the).{0,12}(数据|记录|表|database|table)", re.I), "数据破坏诱导"),
]


def normalize_for_match(text: str) -> str:
    """对抗归一化：零宽字符、全角、空白、大小写。公开供测试与网关复用。"""
    if not text:
        return ""
    cleaned = text.translate(_ZERO_WIDTH)
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = re.sub(r"[\s\u3000]+", " ", cleaned)
    return cleaned.strip().lower()


def detect_injection(text: str) -> list[str]:
    """返回命中的违规描述列表；空列表表示未检出。永不抛异常。"""
    try:
        normalized = normalize_for_match(text or "")
        if not normalized:
            return []
        findings: list[str] = []
        for pattern, label in _PATTERNS:
            if pattern.search(normalized):
                if label not in findings:
                    findings.append(label)
        return findings
    except Exception:  # 检测器自身故障时放行并由合规节点兜底
        return []

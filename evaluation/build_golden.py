"""从知识库自动生成评估集（Golden Set）。

3 篇文档 × 38 条用例的规模无法暴露真实召回问题。本模块按文档逐篇生成
用户问法，得到与语料规模匹配的标注集，并补充难负例与超范围用例。

标注可靠性：
- expected_source 直接取自文档路径，不依赖模型判断（避免标注漂移）
- expected_keywords 由程序从文档正文抽取并校验为真实子串
- 模型只负责"把文档改写成用户会怎么问"，不负责判断答案

用法：
    python -m evaluation.build_golden --out evaluation/datasets/v2.jsonl
    python -m evaluation.build_golden --per-doc 2 --concurrency 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

QUERY_PROMPT = """下面是客服知识库中的一篇文档。请生成 {n} 条**真实用户会问的问题**，
这些问题的答案应当能在该文档中找到。

要求：
1. 用口语化中文，像真实客户打字提问（可以有省略、口头语）
2. {n} 条之间角度不同：一条问核心规则，一条问具体细节或边界条件
3. 不要直接照抄文档标题，要换成用户的说法
4. 每条不超过 30 字
5. 不要问文档里没有的内容

文档标题：{title}
文档正文：
{content}

只输出 JSON 数组，例如 ["问题一", "问题二"]，不要任何解释。"""

#: 超范围用例：与业务相关但知识库确实没有的内容（难负例）
OUT_OF_SCOPE_QUERIES = [
    "今天上证指数收盘多少点",
    "帮我预测下周黄金价格走势",
    "你们公司CEO是谁",
    "美股开户怎么弄",
    "比特币现在什么价",
    "帮我写一段Python爬虫代码",
    "附近哪里有你们的线下网点营业到几点",
    "你们员工的工资待遇怎么样",
    "明天北京天气如何",
    "推荐几只必涨的股票",
    "帮我算一下房贷月供",
    "美元兑日元汇率现在多少",
    "你们和支付宝是什么关系",
    "怎么才能成为你们的代理商",
    "帮我查一下我的征信报告",
]

#: 寒暄/能力类：不该匹配任何知识文档
CONVERSATIONAL_QUERIES = [
    "你好",
    "在吗",
    "谢谢你的帮助",
    "你能做什么",
    "再见",
    "你是机器人吗",
    "好的明白了",
    "辛苦了",
]


def extract_keywords(content: str, title: str, limit: int = 4) -> list[str]:
    """从文档抽取可校验的关键词（必须是正文真实子串）。

    优先取标题中的实词与正文中的数字型规则（金额/天数/比例），
    这类词在检索命中判定里最有区分度。
    """
    found: list[str] = []
    # 数字型规则：7天、3-5个工作日、10000元、R2 等
    for m in re.finditer(r"\d+(?:-\d+)?(?:个)?(?:工作日|天|小时|分钟|元|万元|%|折)|R[1-5]", content):
        token = m.group()
        if token not in found:
            found.append(token)
        if len(found) >= limit:
            return found
    # 标题实词（去掉序号与常见虚词）
    for token in re.findall(r"[\u4e00-\u9fff]{2,6}", title):
        if token in content and token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


def _parse_queries(text: str) -> list[str]:
    raw = (text or "").strip()
    fence = chr(96) * 3
    if raw.startswith(fence):
        raw = raw[len(fence):]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
        if raw.endswith(fence):
            raw = raw[: -len(fence)].strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # 兜底：按行抽取
        return [ln.strip(" -\"'") for ln in raw.splitlines() if ln.strip()][:3]
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    if isinstance(data, dict):
        for key in ("queries", "questions", "data"):
            if isinstance(data.get(key), list):
                return [str(x).strip() for x in data[key] if str(x).strip()]
    return []


async def generate_for_doc(
    llm: Any, doc: Any, per_doc: int, semaphore: asyncio.Semaphore
) -> list[dict]:
    from langchain_core.messages import HumanMessage

    prompt = QUERY_PROMPT.format(
        n=per_doc, title=doc.title, content=doc.content[:1500]
    )
    async with semaphore:
        try:
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            queries = _parse_queries(getattr(resp, "content", "") or "")
        except Exception as exc:  # 单篇失败不影响整体
            logger.warning("生成失败 %s: %s", doc.source, type(exc).__name__)
            return []

    keywords = extract_keywords(doc.content, doc.title)
    out: list[dict] = []
    for q in queries[:per_doc]:
        if not q or len(q) > 60:
            continue
        out.append({
            "query": q,
            "expected_source": doc.source,
            "expected_keywords": keywords,
            "reference_answer": doc.content[:200],
            "tags": [doc.category, "generated"],
        })
    return out


async def build_golden(
    kb_dir: str | Path,
    llm: Any,
    *,
    per_doc: int = 2,
    concurrency: int = 5,
    include_oos: bool = True,
    include_conversational: bool = True,
) -> list[dict]:
    from memory.kb_loader import load_documents

    docs = load_documents(kb_dir)
    if not docs:
        raise ValueError(f"知识库为空: {kb_dir}")

    sem = asyncio.Semaphore(max(1, concurrency))
    tasks = [generate_for_doc(llm, d, per_doc, sem) for d in docs]
    logger.info("开始生成：%d 篇 × %d 条，并发 %d", len(docs), per_doc, concurrency)
    results = await asyncio.gather(*tasks)

    cases: list[dict] = []
    for batch in results:
        cases.extend(batch)

    if include_oos:
        for q in OUT_OF_SCOPE_QUERIES:
            cases.append({
                "query": q, "expected_source": "out_of_scope",
                "expected_keywords": [], "reference_answer": "",
                "tags": ["out_of_scope", "hard_negative"],
            })
    if include_conversational:
        for q in CONVERSATIONAL_QUERIES:
            cases.append({
                "query": q, "expected_source": "conversation_general",
                "expected_keywords": [], "reference_answer": "",
                "tags": ["conversation_general"],
            })

    for i, case in enumerate(cases, start=1):
        case["id"] = f"kb-{i:04d}"
    return cases


def write_jsonl(cases: list[dict], out_path: str | Path) -> int:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    return len(cases)


async def _main(args: Any) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    from agents.provider import build_chat_model
    from config import Settings
    from memory.kb_loader import corpus_stats, load_documents

    settings = Settings()
    docs = load_documents(args.kb)
    print("语料统计:", json.dumps(corpus_stats(docs), ensure_ascii=False))

    llm = build_chat_model(settings, tier="light")
    cases = await build_golden(
        args.kb, llm, per_doc=args.per_doc, concurrency=args.concurrency
    )
    n = write_jsonl(cases, args.out)

    from collections import Counter
    by_src = Counter(
        "out_of_scope" if c["expected_source"] == "out_of_scope"
        else "conversation_general" if c["expected_source"] == "conversation_general"
        else "grounded"
        for c in cases
    )
    print(f"已写入 {n} 条 -> {args.out}")
    print("分布:", dict(by_src))
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="从知识库生成评估集")
    parser.add_argument("--kb", default="knowledge_base")
    parser.add_argument("--out", default="evaluation/datasets/v2.jsonl")
    parser.add_argument("--per-doc", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()

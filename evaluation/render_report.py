"""把检索基准 JSON 渲染成详细 Markdown 报告。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

BT = chr(96)
FENCE = BT * 3


def _code(s: str) -> str:
    return BT + s + BT


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _delta(a: float, b: float) -> str:
    d = (b - a) * 100
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.2f}pp"


def render(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    corpus = payload.get("corpus", {})
    reports = payload.get("reports", [])
    sweep = payload.get("threshold", {})

    lines.append("# 检索质量基准报告")
    lines.append("")
    lines.append("> 由 " + _code("evaluation.retrieval_bench") + " 生成，"
                 + _code("evaluation.render_report") + " 渲染。")
    lines.append("")
    lines.append("## 1. 语料规模")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 文档数 | {corpus.get('documents', 0)} |")
    lines.append(f"| 总字数 | {corpus.get('total_chars', 0):,} |")
    lines.append("| 单篇字数 min/p50/max | "
                 f"{corpus.get('chars_min', 0)} / {corpus.get('chars_p50', 0)} / "
                 f"{corpus.get('chars_max', 0)} |")
    cats = corpus.get("categories", {})
    lines.append("| 分类分布 | " + ", ".join(f"{k}={v}" for k, v in cats.items()) + " |")
    ingest = payload.get("ingest", {})
    lines.append(f"| 入库块数 | {ingest.get('chunks', 0)} |")
    lines.append(f"| 评估用例 | {payload.get('cases', 0)} |")
    lines.append("")

    if reports:
        lines.append("## 2. 检索指标")
        lines.append("")
        keys = ["recall@1", "recall@3", "recall@5", "recall@10", "mrr", "ndcg@5"]
        lines.append("| 配置 | " + " | ".join(keys) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in keys) + " |")
        for rep in reports:
            m = rep.get("metrics", {})
            lines.append(f"| {rep.get('label', '')} | "
                         + " | ".join(_pct(m.get(k, 0.0)) for k in keys) + " |")
        lines.append("")
        base = reports[0]
        lines.append(f"有效召回用例（grounded）：**{base.get('grounded', 0)}** 条"
                     "（寒暄与超范围用例不进召回分母）。")
        lines.append("")
        if len(reports) > 1:
            lines.append("### 重排增益")
            lines.append("")
            a, b = reports[0].get("metrics", {}), reports[1].get("metrics", {})
            lines.append("| 指标 | 基线 | +重排 | 变化 |")
            lines.append("| --- | --- | --- | --- |")
            for k in keys:
                lines.append(f"| {k} | {_pct(a.get(k, 0))} | {_pct(b.get(k, 0))} | "
                             f"{_delta(a.get(k, 0), b.get(k, 0))} |")
            lines.append("")
            la = reports[0].get("latency_ms", {})
            lb = reports[1].get("latency_ms", {})
            lines.append(f"延迟代价：p50 {la.get('p50', 0):.0f}ms → {lb.get('p50', 0):.0f}ms，"
                         f"p95 {la.get('p95', 0):.0f}ms → {lb.get('p95', 0):.0f}ms。")
            lines.append("")

    if reports and reports[0].get("by_category"):
        lines.append("## 3. 分类拆解")
        lines.append("")
        lines.append("| 分类 | 用例 | Recall@1 | Recall@5 | MRR |")
        lines.append("| --- | --- | --- | --- | --- |")
        for cat, m in sorted(reports[0]["by_category"].items()):
            lines.append(f"| {cat} | {int(m.get('cases', 0))} | "
                         f"{_pct(m.get('recall@1', 0))} | {_pct(m.get('recall@5', 0))} | "
                         f"{m.get('mrr', 0):.4f} |")
        lines.append("")

    if sweep and "best" in sweep:
        lines.append("## 4. 置信度阈值工作点")
        lines.append("")
        hit, miss = sweep.get("hit", {}), sweep.get("miss", {})
        lines.append("| 样本 | n | min | p50 | max |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.append(f"| 应命中 | {hit.get('n', 0)} | {hit.get('min')} | "
                     f"{hit.get('p50')} | {hit.get('max')} |")
        lines.append(f"| 应转人工 | {miss.get('n', 0)} | {miss.get('min')} | "
                     f"{miss.get('p50')} | {miss.get('max')} |")
        lines.append("")
        best = sweep["best"]
        lines.append(f"**最优阈值 {best['threshold']}**，标注集准确率 {_pct(best['accuracy'])}"
                     f"（TP={best['tp']} FN={best['fn']} FP={best['fp']} TN={best['tn']}）。")
        lines.append("")
        if sweep.get("separable"):
            lines.append("两类分数**完全可分**，阈值可稳定区分。")
        else:
            lines.append("⚠ 两类分数**存在重叠**：单靠向量阈值无法完全判别，"
                         "建议叠加重排分数或 LLM 判定做二次门禁。")
        lines.append("")

    if reports and reports[0].get("failures"):
        lines.append("## 5. 失败样例（未进 Top-5）")
        lines.append("")
        lines.append("| 用例 | 查询 | 期望文档 | 排名 | 实际 Top-1 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in reports[0]["failures"][:15]:
            rank = f.get("rank", 0)
            rank_s = str(rank) if rank else "未命中"
            lines.append(f"| {f.get('id', '')} | {f.get('query', '')[:24]} | "
                         f"{f.get('expected', '')} | {rank_s} | {f.get('top1', '')} |")
        lines.append("")

    lines.append("## 复核")
    lines.append("")
    lines.append(FENCE + "bash")
    lines.append("make kb-stats     # 语料规模")
    lines.append("make kb-golden    # 重新生成评估集")
    lines.append("make kb-bench     # 重跑基准（含重排 A/B）")
    lines.append(FENCE)
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="渲染检索基准报告")
    parser.add_argument("--json", default="var/bench.json")
    parser.add_argument("--out", default="docs/RETRIEVAL-BENCHMARK.md")
    args = parser.parse_args()
    payload = json.loads(Path(args.json).read_text(encoding="utf-8"))
    text = render(payload)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"报告已写入 {out}（{len(text)} 字符）")


if __name__ == "__main__":
    main()
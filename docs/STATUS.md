# 实现状态（对照 UPGRADE-PLAN-v2 任务表）

> 版本 v0.4.0 · 测试 365 项（全离线）· 覆盖率约 82% · ruff 全绿
> 状态定义：✅ 完成 ｜ 🟡 部分完成（有明确偏差）｜ ⏸ 阻塞（缺外部输入）｜ ⛔ 未启动

## 总览

| 阶段 | 任务数 | ✅ | 🟡 | ⏸ | ⛔ |
| --- | --- | --- | --- | --- | --- |
| P0 正确性与安全 | 11 | 11 | 0 | 0 | 0 |
| P1 规范对齐 | 9 | 7 | 2 | 0 | 0 |
| P2 能力现代化 | 8 | 6 | 0 | 2 | 0 |
| P3 按需扩展 | 6 | 1 | 0 | 0 | 5 |

---

## P0 — 正确性与安全（11/11）

| ID | 任务 | 状态 | 证据 |
| --- | --- | --- | --- |
| T-P0-01 | 注入检测前置 | ✅ | `security/injection.py`；`api/main.py` 的 `_guard_message` 在构图前拦截；`tests/test_compliance.py`、`tests/test_guardrail.py` |
| T-P0-02 | 合规 fail-closed + 逐键脱敏 + PII 校验 | ✅ | `agents/compliance_checker.py`；Luhn / GB11643；`tests/test_compliance.py`（21 项） |
| T-P0-03 | 审批外置 + 哈希链审计 | ✅ | `security/approvals.py`；`tests/test_approvals.py`、`tests/test_approvals_redis.py` |
| T-P0-04 | 删除 FAQ 早退与跨用户缓存 | ✅ | `agents/supervisor.py`（无模块级缓存） |
| T-P0-05 | embedding 快速失败 | ✅ | `memory/long_term.py` 的 `EmbeddingUnavailableError`；`tests/test_memory.py` |
| T-P0-06 | 知识库写入治理 | ✅ | 移除对话原文入库；`save()` 在 lifespan 接入；doc_id 去重 |
| T-P0-07 | 接口设防 | ✅ | 鉴权 / 限流 / 长度 / CORS；`tests/test_api.py`、`tests/test_admin_rate_limit.py` |
| T-P0-08 | 死代码与配置接线 | ✅ | 删除 `intent_router.py`、`working_memory.py`、`checkpoint_retention_days` |
| T-P0-09 | 测试与评估基线 | ✅ | 292 项测试；`evaluation/datasets/v1.jsonl`（38 条） |
| T-P0-10 | 异步边界修复 | ✅ | `asearch` + `asyncio.to_thread`；`tests/test_memory.py` |
| T-P0-11 | 容器与运行时快修 | ✅ | `Dockerfile`：多阶段、非 root、HEALTHCHECK、无 `--reload` |

## P1 — 规范对齐（7 ✅ / 2 🟡）

| ID | 任务 | 状态 | 说明与证据 |
| --- | --- | --- | --- |
| T-P1-01 | 依赖与结构迁移 | 🟡 | `pyproject.toml` + 依赖区间 ✅；**未迁 `src/`、无 `uv.lock`**（本机装不上 uv，见 ADR-0006） |
| T-P1-02 | 编排 1.x | 🟡 | 保留确定性 StateGraph（ADR-0001）；工具调用走 `create_agent`（`agents/react_agent.py`），未整体替换 Supervisor |
| T-P1-03 | 结构化输出 | ✅ | `agents/schemas.py` + `agents/llm_utils.py`；路由 / 工单 / 合规三处接入；`tests/test_structured_output.py` |
| T-P1-04 | 模型接入 | ✅ | `agents/provider.py` 支持 Responses API 开关、推理强度、prompt cache 槽位、stream_usage；参数按运行时签名过滤。**已用真实供应商验证**（glm-5.3-flash，见 REAL-MODEL-VALIDATION.md）；新增 `STRUCTURED_OUTPUT_ENABLED` 应对不支持 response_format 的代理 |
| T-P1-05 | 检查点 + HITL | ✅ | `interrupt()` / `Command(resume)`；`AsyncSqliteSaver` / `AsyncPostgresSaver`；ADR-0005 |
| T-P1-06 | MCP 官方 SDK | ✅ | `tooling/`；`mcp/` 已改名消除遮蔽；1.x/2.x 双兼容；`tests/test_mcp.py`（14 项） |
| T-P1-07 | 工具接入 Agent 链路 | ✅ | 工单动作由 `tool_calls` 驱动；知识检索经 `knowledge_search` 工具（后端在启动时注入真实向量库，失败自动回退直连）；两条链路都有 `execute_tool` span |
| T-P1-08 | GenAI 语义约定 | ✅ | `tracing/otel_config.py`；`tests/test_tracing.py`（15 项） |
| T-P1-09 | 文档与 ADR | ✅ | 8 条 ADR + README + CONTRIBUTING + CHANGELOG + 本文件 |

## P2 — 能力现代化（6 ✅ / 2 ⏸）

| ID | 任务 | 状态 | 说明 |
| --- | --- | --- | --- |
| T-P2-01 | 检索质量 | ✅ | Contextual Retrieval、CJK 混合分词；**阈值已用真实分布校准 0.30→0.53**（准确率 84.38%→90.62%）；**API 重排已启用**（`memory/rerank_api.py`，bge-reranker-v2-m3） |
| T-P2-02 | 记忆分域 | ✅ | `memory/context_store.py`，org/user 命名空间；ADR-0008 |
| T-P2-03 | 向量库后端 | ⏸ | 需先评估数据量；当前 FAISS 足够 |
| T-P2-04 | 评估体系 | ✅ | 路由 / 合规 / 检索 / 执行轨迹指标；`evaluation/trajectory_eval.py` 从 span 还原阶段序列；`tracing/eval_link.py` 把指标以 `gen_ai.evaluation.result` span 回挂 trace（`--emit-eval-events`） |
| T-P2-05 | CI 门禁 | ⏸ | 模板 `docs/ci/ci.yml.example` 就绪；GitHub token 缺 `workflow` scope 无法推送。本地已有等价物：`make hooks`（pre-commit）+ `make ci`（ruff → pytest → 评估门禁） |
| T-P2-06 | 流式与前端协议 | ✅ | 节点级 + token 级 SSE；AG-UI 子集（`events/agui.py`，⚠ 未对照官方 SDK 复核字段） |
| T-P2-07 | 工程与多副本 | ✅ | compose / redis 限流 / redis 审批 / Postgres 检查点 ✅；指标经 OTel Metrics + OTLP 可跨副本聚合（`OTEL_METRICS_ENABLED=true`） |
| T-P2-08 | 模型化护栏 | ✅ | `security/guardrail.py`（默认关闭，需配置 endpoint） |

## P3 — 按需扩展（1 ✅ / 5 ⛔）

| ID | 任务 | 状态 | 说明 |
| --- | --- | --- | --- |
| T-P3-01 | A2A | ⛔ | 无跨组织互调需求 |
| T-P3-02 | 向量库迁移 | ⛔ | 同 T-P2-03 |
| T-P3-03 | 前端组件化 / 审批台 | ⛔ | 当前单文件 UI + 聊天内审批卡片（最小可用） |
| T-P3-04 | 成本优化 | ✅ | 模型分级（路由走轻模型）+ 语义缓存（`memory/semantic_cache.py`，ADR-0009）+ 每用户自然日预算 `security/budget.py` |
| T-P3-05 | MCP Tasks | ⛔ | 无长任务场景 |
| T-P3-06 | 多模态 | ⛔ | 无语音/图片工单需求 |

---

## 已知缺口（按影响排序）

1. **真实链路已全面验证**（2026-11-07，见 [REAL-MODEL-VALIDATION.md](REAL-MODEL-VALIDATION.md)）：LLM 路由 96.67%、工具调用生效、合规语义级识别；embedding（Qwen3-Embedding-0.6B，dim 1024）与 API 重排（bge-reranker-v2-m3）已接入，阈值校准为 0.53。**注意**：知识库仅 3 篇文档，检索指标满分不具区分度；两类分数仍重叠约 9%，单靠向量阈值不足以完全判别。
2. **CI 未启用**：仅本地 pre-commit 生效。
3. ~~工单双存储~~：已偿还（`persistence/ticket_repo.py` 统一仓储，ADR-0007 已关闭）。
4. **AG-UI 字段未复核**：官方 SDK 源码不可达，接入真实客户端前需核对。
5. ~~指标不跨副本~~：已解决（`OTEL_METRICS_ENABLED=true` + OTLP 导出，T-P2-07）。`/metrics` 端点仍为单进程视图，跨副本聚合走 collector。
6. **语义缓存为进程内实现**：多副本需换 Redis 版；`kb_version` 用文档数近似（ADR-0009）。

## 如何复核

```bash
python -m pytest -q                     # 365 项
python -m ruff check . --exclude docs   # 全绿
python -m evaluation.run                # 路由 + 合规门禁
make calibrate                          # 检索阈值校准（需真实数据才有意义）
make hooks                              # 启用本地 pre-commit
```

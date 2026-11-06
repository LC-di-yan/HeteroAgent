# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号语义化。

## [0.4.0] — 2026-11-06 · P2 能力现代化

### 新增
- 检索：Contextual Retrieval 入库（`memory/ingest.py`）；BM25 改「单字 + 双字」混合分词
- 记忆：跨会话用户事实（LangGraph Store，org/user 作用域），工单号自动落库并注入 RAG 提示词
- 评估：路由质量评估（30 条标注集）、统一门禁 `evaluation/run.py`、检索阈值校准 `evaluation/calibrate.py`
- 安全：可插拔护栏链（规则 + 模型分类器，含超时/fail-closed 策略）；管理端点限流
- 观测：Prometheus 文本指标 `/metrics`；JSON 结构化日志 + request_id 贯穿
- 协议：AG-UI 事件子集（`?protocol=agui`）；token 级 SSE 增量
- 运维：docker-compose（app + redis + jaeger）、Makefile、DEPLOY 手册
- 体验：离线演示模式（`DEMO_MODE`，无 API Key 跑通全链路）；聊天流内直接审批

### 变更
- 模型分级：路由使用 `LIGHT_MODEL_NAME`（此前为死配置）
- 检查点：异步图改用 `AsyncSqliteSaver`（修复同步 saver 在 `ainvoke` 下崩溃）

### 移除
- `memory/working_memory.py`（死代码，已由 Store 取代）

## [0.3.0] — 2026-11-06 · P1 规范对齐

### 新增
- MCP 官方 SDK（`tooling/`，替代自造 JSON-RPC；消除与 PyPI `mcp` 的 import 遮蔽）
- 图内 HITL：`interrupt()` + `Command(resume)`，SQLite/Postgres 检查点
- 结构化输出：RouteDecision / TicketAction / ComplianceVerdict 等 Pydantic schema
- 工具调用：`create_agent` 工厂 + `execute_tool` span；工单动作由 `tool_calls` 驱动
- OTel GenAI 语义约定（`gen_ai.*`）、token 与成本累计
- ADR 目录（8 条关键决策）

## [0.2.0] — 2026-11-06 · P0 正确性与安全

### 修复（G1–G14）
- 注入检测前置到 API 入口（任一 LLM 调用之前）
- 合规审查 fail-closed；逐子结果分别脱敏；PII 增加 Luhn / GB11643 校验
- 审批外置 + 哈希链审计；不再回传被拦截原文；删除编造工单号的文案
- 删除 FAQ 早退与跨用户路由缓存；embedding 失败快速报错（禁止伪向量）
- 接口设防：API Key、限流、请求体/消息长度上限、CORS 白名单
- 检索分数归一化到 [0,1]（此前与阈值不同量纲）

### 新增
- 180+ 项离线测试；`pyproject.toml` + 依赖区间；多阶段非 root 镜像；`.env.example` 46 项

## [0.1.0] — 初始版本

LangGraph Supervisor 多 Agent 客服原型：意图路由、RAG、工单、两阶段合规、三层记忆、MCP 工具、OTel 追踪。

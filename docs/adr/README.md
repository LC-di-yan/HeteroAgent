# 架构决策记录（ADR）

记录"为什么这样设计"，避免后续维护者在不了解约束的情况下推翻决策。
新增决策时复制 `0000-template.md`，编号递增，状态取值：提议 / 已接受 / 已废弃 / 被取代。

| 编号 | 决策 | 状态 |
| --- | --- | --- |
| [0001](0001-fixed-supervisor-topology.md) | 保留固定 Supervisor 状态图，不做自主 Agent 循环 | 已接受 |
| [0002](0002-compliance-fail-closed.md) | 合规审查 fail-closed | 已接受 |
| [0003](0003-no-fake-vector-fallback.md) | 禁止伪向量降级 | 已接受 |
| [0004](0004-official-mcp-sdk.md) | 采用官方 MCP SDK，仓库包改名 tooling/ | 已接受 |
| [0005](0005-interrupt-based-hitl.md) | 用 interrupt()/Command(resume) 实现人工审批 | 已接受 |
| [0006](0006-layout-and-dependency-policy.md) | 暂不迁 src/ 布局；依赖区间 + 后续加锁 | 已接受 |
| [0007](0007-transitional-dual-ticket-path.md) | 工单双实现（内存 TicketStore 与 MCP 工具）的过渡安排 | 已接受 |
| [0008](0008-context-store-facts-only.md) | 跨会话记忆只存确定性事实，不做 LLM 抽取 | 已接受 |

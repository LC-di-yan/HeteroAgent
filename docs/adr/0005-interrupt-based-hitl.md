# 0005. 用 interrupt()/Command(resume) 实现人工审批

- 状态：已接受
- 日期：2026-11-06
- 相关：`agents/supervisor.py` 的 `compliance_gate`，方案 T-P1-05

## 背景

P0 阶段的审批是"API 层外置队列"：图已经跑完，审批通过只是把文本拼回去返回。
问题：进程重启即丢、多副本不共享、审批后无法真正"继续执行"。

## 决策

- 合规未通过时在图内 `interrupt()` 暂停，检查点持久化（默认 SQLite，可切 Postgres）；
- 审批调用 `Command(resume={"approved":...})` 恢复执行；
- 放行后的回复由 `synthesize` 重新生成，并强制 `sanitize_output`（清洗违规用语 + PII）；
- 无检查点时（如单测/无状态场景）自动退回 P0 的外置队列，保证兼容。

## 后果

- 正面：审批语义真实（暂停/恢复），可跨重启、可多副本（共享 DB 时）。
- 负面：检查点会增长，需要保留策略（`CHECKPOINT_RETENTION_DAYS`）；
  审批线程与租户绑定，thread_id 规则必须稳定（`{tenant}:{session}`）。

## 备选方案

- 外部队列 + 事务性重放：实现复杂且容易与图状态不一致，否决。

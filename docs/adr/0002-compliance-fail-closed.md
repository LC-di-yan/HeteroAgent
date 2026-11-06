# 0002. 合规审查 fail-closed

- 状态：已接受
- 日期：2026-11-06
- 相关：`agents/compliance_checker.py`，缺陷 G2，方案 T-P0-02

## 背景

原实现中，LLM 合规审查返回非法 JSON 时按"通过"处理（fail-open）。
在金融场景，这等价于"审查器坏了就放行"。

## 决策

任何无法判定合规性的情况——JSON 解析失败、schema 校验失败、LLM 调用异常、超时——一律：
1. `passed=False`、`risk_level="high"`；
2. 进入人工审批队列（HITL），由人裁决；
3. 审计链记录原因。

## 后果

- 正面：不存在"审查器失效导致违规内容外发"的路径。
- 负面：模型故障时用户体验下降（转人工）；需要人工值班能力。
- 指标：合规误杀率需在评估集上跟踪（`evaluation/evaluator.py` 的 coverage/误杀口径）。

## 备选方案

- fail-open + 告警：事故窗口仍然存在，否决。

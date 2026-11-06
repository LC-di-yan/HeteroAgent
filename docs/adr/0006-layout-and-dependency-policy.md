# 0006. 暂不迁 src/ 布局；依赖用区间约束，后续再加锁

- 状态：已接受
- 日期：2026-11-06
- 相关：`pyproject.toml`、`requirements.txt`，方案 T-P1-01

## 背景

方案原计划迁到 `src/heteroagent/` 并提交 `uv.lock`。实际约束：

1. 本机无 `uv`，无 build-system；仓库不发布 wheel；
2. 所有模块用顶层绝对导入（`from agents.supervisor import ...`），
   迁到 `src/heteroagent/` 需要重写全部导入或引入兼容 shim；
3. 当前阶段收益主要是"根目录整洁 + 防遮蔽"，而 `mcp/` 遮蔽问题已通过改名 `tooling/` 解决。

## 决策

- 维持扁平布局（顶层包：`api/ agents/ memory/ security/ tracing/ evaluation/ tooling/`）；
- 依赖区间写死在 `pyproject.toml`（下界=验证版本，上界=下一大版本），`requirements.txt` 为镜像；
- 不提交 `uv.lock`；等 CI（P2）需要可复现构建时再生成；
- 若未来要发布/被其他项目引用，再执行 `src/heteroagent/` 迁移，届时一并加 `[build-system]`。

## 后果

- 正面：零迁移风险，169+ 测试不需要改动；导入路径简单。
- 负面：根目录与配置混杂；无锁文件时依赖漂移只能靠区间约束兜底。
- 触发条件：出现"需要被其他仓库安装/引用"或"CI 需严格可复现构建"的需求。

## 备选方案

- 立即迁移 src/：机械改动量大、收益有限，风险高于收益。

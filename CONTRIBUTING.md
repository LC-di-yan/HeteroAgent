# 贡献指南

## 环境

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov ruff fakeredis
cp .env.example .env    # 填写 OPENAI_API_KEY（或使用 DEMO_MODE=true 免密钥演示）
```

## 提交前必跑

```bash
python -m ruff check . --exclude docs   # 必须 0 告警
python -m pytest -q                     # 必须全绿（当前 290 项，全离线）
```

涉及检索/合规/路由的改动，还需：

```bash
python -m evaluation.run                # 门禁：路由准确率、合规漏放/误杀
```

## 代码约定

- **配置只走 `config.py` 的 Settings**，禁止散落 `os.getenv`；新增配置项要同步 `.env.example`
- **失败要显式**：不允许"降级成看似可用"（例如 embedding 不可用时不得生成伪向量）
- **有副作用的动作用 schema 约束**（`agents/schemas.py`），解析失败返回 `None`，不许默认建单/放行
- **异步边界**：async 路径里不做同步网络/重 CPU 推理（用 `asyncio.to_thread` 或异步客户端）
- **测试离线**：新增测试不得依赖网络或真实 API Key

## 结构性问题先写 ADR

涉及以下决策时，先在 `docs/adr/` 加一条记录（模板见 `0000-template.md`）：
拓扑变更、状态存储、协议选型、安全策略、引入新依赖或外部服务。

## 提交信息

`type(scope): 描述`，type 取 `feat|fix|refactor|test|docs|chore|perf|security`。
一个提交只做一件事；涉及行为变更请在正文说明"为什么"与"如何回滚"。

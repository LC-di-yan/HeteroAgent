# ══════════════════════════════════════════════════════════════════════
# HeteroAgent 本地开发与部署入口（T-P2-05 / T-P2-07 基础设施部分）
#
# 需要 GNU make。Windows 开发者若没有 make，每个目标上方的注释都给出了
# 直接可粘贴的等价命令（PowerShell / cmd 通用，工作目录=仓库根）。
# 覆盖变量示例：make run PORT=9000；make compose-up COMPOSE=docker-compose
# （后者的宿主机只装了独立版 docker-compose、没有 "docker compose" 子命令时用）。
#
# 注意：所有命令行必须以 Tab 开头（GNU make 硬性要求），编辑时勿把 Tab 改空格。
# ══════════════════════════════════════════════════════════════════════

PY ?= python
HOST ?= 127.0.0.1
PORT ?= 8000
IMAGE ?= heteroagent:0.3.0
COMPOSE ?= docker compose

.DEFAULT_GOAL := install

.PHONY: install run dev test lint format cov hooks check ci docker-build compose-up compose-down eval eval-all route-eval trajectory-eval calibrate kb-stats kb-golden kb-bench

# 安装运行时依赖 + 开发工具（比 CI 多装 pytest-cov，供 cov 目标使用）
# 等价命令：python -m pip install -r requirements.txt pytest pytest-asyncio pytest-cov ruff
install:
	$(PY) -m pip install -r requirements.txt pytest pytest-asyncio pytest-cov ruff

# 启动 API（生产模式，无热重载；绑定 127.0.0.1 只暴露给本机）
# 等价命令：python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
run:
	$(PY) -m uvicorn api.main:app --host $(HOST) --port $(PORT)

# 开发模式：--reload 热重载。仅限本机开发，严禁进容器/生产（Dockerfile 已禁止）
# 等价命令：python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
dev:
	$(PY) -m uvicorn api.main:app --host $(HOST) --port $(PORT) --reload

# 全量离线单测（无需 API key；与 CI lint-test 同一条命令）
# 等价命令：python -m pytest -q
test:
	$(PY) -m pytest -q

# 静态检查（与 CI 完全一致；docs/ 不参与 lint）
# 等价命令：ruff check . --exclude docs
lint:
	$(PY) -m ruff check . --exclude docs

# 统一格式化并自动修复可安全修复的告警（会改写源码，建议提交前单独跑）
# 等价命令：ruff format . ；ruff check . --exclude docs --fix
format:
	$(PY) -m ruff format .
	$(PY) -m ruff check . --exclude docs --fix

# 覆盖率报告（pytest-cov 由 install 目标提供）
# 等价命令：python -m pytest -q --cov=api --cov=agents --cov=memory --cov=security --cov=tooling --cov=tracing --cov=evaluation --cov-report=term-missing
cov:
	$(PY) -m pytest -q --cov=api --cov=agents --cov=memory --cov=security --cov=tooling --cov=tracing --cov=evaluation --cov-report=term-missing

# 构建生产镜像（多阶段、非 root、HEALTHCHECK，见 Dockerfile）
# 等价命令：docker build -t heteroagent:0.3.0 .
docker-build:
	docker build -t $(IMAGE) .

# 起整套本地栈：app + redis + jaeger（后台运行；Jaeger UI: http://localhost:16686）
# 等价命令：docker compose up -d --build ；仅装独立版时：docker-compose up -d --build
compose-up:
	$(COMPOSE) up -d --build

# 停栈并移除容器/网络（数据卷保留；要连 redis-data 一起清：docker compose down -v）
# 等价命令：docker compose down ；仅装独立版时：docker-compose down
compose-down:
	$(COMPOSE) down

# RAG 离线评估（默认 --no-llm 不调判分模型，检索指标照常产出）
# 启用本地 pre-commit 钩子（ruff + pytest）；GitHub CI 未启用时的兜底
# 等价命令：git config core.hooksPath .githooks
hooks:
	git config core.hooksPath .githooks

# 手动跑一遍提交前检查（--fast 只跑 ruff）
# 等价命令：python scripts/check.py
check:
	$(PY) scripts/check.py

# 本地 CI 等价物：与 .github/workflows/ci.yml 同步骤（lint → test → 评估门禁）
# 等价命令：ruff check . --exclude docs && pytest -q && python -m evaluation.run --with-trajectory
ci: lint test
	$(PY) -m evaluation.run --with-trajectory

# 统一门禁（路由 + 合规），退出码非 0 表示低于阈值
# 等价命令：python -m evaluation.run
eval:
	$(PY) -m evaluation.run

# 追加检索维度（需要可用的 embedding 凭据与向量库）
# 等价命令：python -m evaluation.run --with-retrieval --json --out report.json
eval-all:
	$(PY) -m evaluation.run --with-retrieval --json --out report.json

# 只跑路由评估（规则基线），用于快速对比模型路由效果
# 等价命令：python -m evaluation.route_eval
route-eval:
	$(PY) -m evaluation.route_eval

# 执行轨迹评估：验证 路由→业务→合规→闸门→聚合 的路径没被改坏
# 等价命令：python -m evaluation.trajectory_eval
trajectory-eval:
	$(PY) -m evaluation.trajectory_eval

# 校准检索阈值：在标注集上扫描最优值，与当前配置对比
# 等价命令：python -m evaluation.calibrate --threshold $(RAG_THRESHOLD)
calibrate:
	$(PY) -m evaluation.calibrate --threshold $(or $(RAG_THRESHOLD),0.30)

# 知识库规模统计（篇数/分类/字数分布）
# 等价命令：python -c "from memory.kb_loader import *; print(corpus_stats(load_documents()))"
kb-stats:
	$(PY) -c "import json; from memory.kb_loader import load_documents, corpus_stats; print(json.dumps(corpus_stats(load_documents()), ensure_ascii=False, indent=2))"

# 从知识库生成评估集（每篇 2 条用户问法 + 难负例 + 寒暄）
# 等价命令：python -m evaluation.build_golden --out evaluation/datasets/v2.jsonl
kb-golden:
	$(PY) -m evaluation.build_golden --out evaluation/datasets/v2.jsonl --per-doc 2 --concurrency 5

# 检索基准：Recall@K/MRR/nDCG + 分类拆解 + 重排 A/B + 阈值扫描
# 等价命令：python -m evaluation.retrieval_bench --rerank-ab --out var/bench.json
kb-bench:
	$(PY) -m evaluation.retrieval_bench --rerank-ab --out var/bench.json

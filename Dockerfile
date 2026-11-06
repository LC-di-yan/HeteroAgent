# ══════════════════════════════════════════════════════════════════════
# HeteroAgent 生产镜像（T-P0-11 打包治理）
#
# 三阶段：base（解释器）→ builder（依赖装到独立 /install 前缀）→
# runtime（只拷贝 /install 与应用代码，非 root 运行）。
# 分层目的：把 pip 解析、编译、wheel 缓存全部留在 builder，
# runtime 里没有任何构建工具链，也不存在 .env / var / vector_store / tests。
#
# 依赖真相源：pyproject.toml；本镜像按 requirements.txt 安装运行时依赖。
# 构建：docker build -t heteroagent:0.2.0 .
# 运行：docker run --rm -p 8000:8000 --env-file .env heteroagent:0.2.0
# ══════════════════════════════════════════════════════════════════════

# ── Stage 1: builder ── 只用于安装依赖，产物是 /install 目录树
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# 先只拷依赖清单：源码变更不会击穿这一层的缓存
COPY requirements.txt ./

# --prefix=/install → /install/lib/python3.12/site-packages + /install/bin
# 用独立前缀而非直接写 /usr/local，方便 runtime 一次 COPY 带走全部依赖
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.txt --prefix=/install

# ── Stage 2: runtime ── 最小运行面：解释器 + /install + 应用代码 + 数据空目录
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONPATH=/app

# 非 root：系统账号，无 home、不可登录；数据目录预先建好并交属主
RUN groupadd --system appgroup \
 && useradd --system --gid appgroup --home-dir /app --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/var /app/vector_store \
 && chown -R appuser:appgroup /app

WORKDIR /app

# 依赖层（builder 的唯一产物）
COPY --from=builder /install /usr/local

# 应用代码：逐目录显式拷贝，避免把仓库里的开发期垃圾吸进镜像。
# .dockerignore 另外排除 var/、vector_store/、tests/、.env，
# 保证本地索引、检查点库与真实密钥永远不进镜像层。
COPY --chown=appuser:appgroup config.py ./
COPY --chown=appuser:appgroup api/ ./api/
COPY --chown=appuser:appgroup agents/ ./agents/
COPY --chown=appuser:appgroup memory/ ./memory/
COPY --chown=appuser:appgroup security/ ./security/
COPY --chown=appuser:appgroup tracing/ ./tracing/
COPY --chown=appuser:appgroup evaluation/ ./evaluation/
COPY --chown=appuser:appgroup tooling/ ./tooling/
COPY --chown=appuser:appgroup events/ ./events/
COPY --chown=appuser:appgroup static/ ./static/

# 切换到 appuser：此后 CMD / HEALTHCHECK 均以非 root 身份执行
USER appuser

EXPOSE 8000

# 健康检查：slim 镜像没有 curl，用标准库 urllib 打活探针端点。
# 探针目标是 /health/live（存活）；/health/ready 供编排系统判断依赖是否就绪。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

# 单 worker：内存型审批队列 / FAISS 索引 / 检查点句柄都是进程内状态，
# 多 worker 会出现状态分裂；扩容走副本水平扩展，不走 --workers。
# 绝对禁止 --reload：热重载会派生 reloader 子进程、破坏非 root 目录权限与信号处理。
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

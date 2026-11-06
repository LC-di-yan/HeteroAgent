# HeteroAgent 部署手册（T-P2-05 + T-P2-07 基础设施）

> CI 配置以模板形式存放在 [docs/ci/ci.yml.example](ci/ci.yml.example)，
> 启用需先 `gh auth refresh -s workflow`（当前 token 缺 workflow scope，无法推送 workflow 文件），
> 再复制到 `.github/workflows/ci.yml` 并提交。

覆盖四种落地方式：本地 venv、单容器、docker compose 一体化栈、多副本扩缩。
所有环境变量名以根目录 [config.py](../config.py) 的 `Settings` 字段为准，
可复制 [.env.example](../.env.example) 作为起点；本文不重复默认值，只讲怎么跑起来、
怎么验证、跑不起来先看哪里。

| 方式 | 适用场景 | 依赖 | 持久化 |
| --- | --- | --- | --- |
| 本地 venv | 开发、调试、跑测试 | Python 3.12 + Redis（可选） | `./var/checkpoints.db` |
| 单容器 | 单机验证、CI 构建校验 | Docker | 需自行挂载 `/app/var` |
| docker compose | 本地一体化：应用 + Redis + Jaeger | Docker + Compose | 命名卷 `redis-data` + 宿主 `./var` |
| 多副本 | 生产扩容 | 外部 Postgres/Redis/对象存储 | 见「多副本注意事项」 |

---

## 1. 本地 venv 运行

前提：Python 3.12（`pyproject.toml` 约束 `>=3.12,<3.14`）。

```powershell
# Windows PowerShell；bash/zsh 把 ".venv\Scripts\Activate.ps1" 换成 "source .venv/bin/activate"
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pytest pytest-asyncio pytest-cov ruff

Copy-Item .env.example .env
# 编辑 .env：至少填 OPENAI_API_KEY（留空则 ready 探针 503，见第 5 节）
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

有 make 时直接用 [Makefile](../Makefile)（每个目标上方也注释了等价的裸命令）：

```bash
make install          # 装依赖
make run              # 127.0.0.1:8000，无热重载
make dev              # 加 --reload，仅限本地
make test && make lint
```

验证：

```powershell
curl.exe http://127.0.0.1:8000/health/live    # 期望 200 {"status":"live",...}
curl.exe http://127.0.0.1:8000/health/ready   # 期望 200 {"status":"ready",...}
```

不配 Redis 也能跑：`REDIS_URL` 默认 `redis://localhost:6379/0`，短期记忆会走连接失败路径；
离线单测全部使用替身（fakeredis / stub），`python -m pytest -q` 无需任何外部服务。

---

## 2. 单容器 docker run

```bash
docker build -t heteroagent:0.3.0 .
docker run --rm -p 8000:8000 --env-file .env \
  -v "$PWD/var:/app/var" \
  heteroagent:0.3.0
```

镜像事实（[Dockerfile](../Dockerfile)）：多阶段构建、以非 root 的 `appuser` 运行、
`EXPOSE 8000`、`HEALTHCHECK` 打 `/health/live`、`CMD uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1`。

要点：

- **必须挂 `/app/var`**：`CHECKPOINT_PATH` 默认 `./var/checkpoints.db`，不挂则容器重启即丢会话检查点；
  `var/` 已在 `.dockerignore` 中排除，不会把宿主状态烤进镜像层。
- **单 worker 是硬约束**：内存审批队列、FAISS 索引句柄、SQLite 连接都是进程内状态，
  `--workers 2` 会造成状态分裂。
- **`--env-file` 与容器内地址**：`.env` 里的 `REDIS_URL=redis://localhost:6379/0` 在容器内指向容器自身，
  连不上宿主 Redis 时请显式覆盖：`-e REDIS_URL=redis://host.docker.internal:6379/0`。
- 查看健康状态：`docker inspect --format '{{.State.Health.Status}}' <容器名>`。

> ⚠️ 已知阻塞（本文写作时实测）：`Dockerfile` 中有一行 `COPY mcp/ ./mcp/`，
> 而仓库根的实际目录已改名 `tooling/`（[ADR 0006](adr/0006-layout-and-dependency-policy.md)），
> `mcp/` 不存在，因此 **`docker build` 目前会在该层失败**，`make docker-build`、
> CI 的 `package` job 同样失败。修复方式是把该行改为 `COPY --chown=appuser:appgroup tooling/ ./tooling/`。
> 该文件不在本轮改动范围内（本轮只交付 CI/编排/文档），修正前请勿把构建失败误判为编排问题。

---

## 2.4 日志形态

- 默认 `LOG_FORMAT=text`：`2026-11-06 22:30:01 INFO    [<request_id>] api.main: ...`
- 采集型部署设 `LOG_FORMAT=json`：单行 JSON（ts/level/logger/msg/request_id/exc）
- `request_id` 来自请求头 `X-Request-ID`，缺省自动生成并回写响应头；同一请求的
  路由/检索/合规/工具日志可用它串联。

## 2.5 监控接入（Prometheus 抓取）

`GET /metrics` 输出 Prometheus 文本格式（counter/gauge + 分位数标签），无需额外依赖：

```yaml
scrape_configs:
  - job_name: heteroagent
    metrics_path: /metrics
    static_configs:
      - targets: ["heteroagent:8000"]
    # 端点默认需要鉴权；要么配置 METRICS_PUBLIC=true，要么加 bearer/header 注入
```

主要指标：`heteroagent_agent_calls_total`、`heteroagent_agent_errors_total`、
`heteroagent_agent_duration_ms{quantile="0.5|0.95|0.99"}`、`heteroagent_llm_tokens_total{direction}`、
`heteroagent_llm_cost_usd_total`、`heteroagent_cost_usd_total`。

## 3. docker compose 一体化栈

```bash
cp .env.example .env      # 然后填入 OPENAI_API_KEY
mkdir -p var              # compose 挂载 ./var，宿主机上先建出来更干净
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

> 本机若只装了独立版 Compose（没有 `docker compose` 子命令），
> 用 `docker-compose up -d --build`；`make` 层可统一覆盖：`make compose-up COMPOSE=docker-compose`。

编排内容（[docker-compose.yml](../docker-compose.yml)）：

- `app`：`build: .`，`8000:8000`，`env_file: .env` + `environment` 覆盖四个容器网络内地址
  （`REDIS_URL=redis://redis:6379/0`、`APPROVAL_STORE=redis`、
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317`、`CHECKPOINT_PATH=/app/var/checkpoints.db`），
  `volumes: ./var:/app/var`，`depends_on: redis(condition: service_healthy)`。
  健康检查用 `python -c` + urllib 请求 `http://127.0.0.1:8000/health/live`（slim 镜像没有 curl）。
- `redis`：`redis:8-alpine`，`redis-cli ping` 健康检查，命名卷 `redis-data:/data`，
  **不映射宿主端口**（无密码，仅容器网络可达）。
- `jaeger`：`jaegertracing/all-in-one:latest`，`16686`（UI）+ `4317`（OTLP gRPC），
  `COLLECTOR_OTLP_ENABLED=true`。

### 3.1 看 trace：Jaeger UI

浏览器打开 <http://localhost:16686> → 左上角 Service 选 `heteroagent`（来自 `OTEL_SERVICE_NAME`）
→ Find Traces。发一轮对话：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"我的订单还没发货","user_id":"u_demo"}'
```

看不到 span 的常见原因：`OTEL_EXPORTER_OTLP_ENDPOINT` 为空（不导出）；或本地 venv 运行时
端口写成 HTTP 而非 gRPC —— Jaeger 收 OTLP/gRPC 用 **4317**，OTLP/HTTP 用 4318。
只想在终端看 trace 时设 `OTEL_CONSOLE_EXPORT=true`（仅限本地排查，别带去生产）。

### 3.2 验证就绪与存活

```bash
curl -i http://localhost:8000/health/live     # 200 → 进程活着（不依赖外部资源）
curl -i http://localhost:8000/health/ready    # 200 → graph + 向量库都就绪
docker compose exec app python -c "import sys,urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=4); \
print(r.status, r.read().decode()); sys.exit(0 if r.status==200 else 1)"
```

`/health/ready` 判据是 `app.state.graph` 与 `app.state.ltm` 均非空（`api/main.py:357-362`）：
任一为空返回 **503**，detail 里逐子项给出 `{"ready":false,"graph":bool,"vector_store":bool}`，
据此能直接判断是 LLM 侧还是向量库侧没起来。 `/health` 为兼容保留的旧端点（恒 200），
负载均衡请用 `/health/live` + `/health/ready` 两个探针。

停止与清场：

```bash
docker compose down          # 保留 redis-data 与 ./var
docker compose down -v       # 连 Redis 数据卷一起删（./var 里的检查点不会被删）
```

---

## 4. 多副本注意事项

compose 默认**单副本**。加副本（`docker compose up -d --scale app=2`）前必须逐项确认：

1. **审批存储必须走 Redis**：`APPROVAL_STORE=redis`。`memory` 是进程内队列，
   副本 A 发起的待审批在副本 B 查不到、也恢复不了，HITL 直接失联。
   compose 已在 `environment` 里强制 `APPROVAL_STORE=redis`，本地 venv 请自行设置。
2. **检查点会持续增长**：LangGraph 的检查点表不带时间戳，应用层无法精确裁剪，
   因此没有 retention 配置项。运维手段：SQLite 单副本可在无待审批单时直接轮换
   `var/checkpoints.db`；Postgres 用 SQL 按 `thread_id` 清理（先归档再删），
   或定期重建实例。上线前请把这条写进值班手册。
3. **检查点不能是本地 SQLite**：`CHECKPOINT_PATH` 指向各副本私有的 `./var/checkpoints.db` 时，
   同一 `session_id` 的第二个请求被路由到另一副本就读不到中断状态，会话/工单会丢。
   - 现成支持：`CHECKPOINT_BACKEND=postgres` + `POSTGRES_DSN=postgresql://...`；
     `agents/supervisor.py` 的 `_build_checkpointer()` 用 psycopg 建长连接并调用
     `PostgresSaver.setup()`（幂等建表）。缺包或 DSN 为空会抛明确错误，不静默降级。
   - 需额外安装（可选依赖）：`pip install langgraph-checkpoint-postgres`
     （见 `pyproject.toml` 的 `[project.optional-dependencies].postgres`）。
   - 异步图注意：`api` 启动时用 `AsyncSqliteSaver`（异步图不能配同步 `SqliteSaver`，
     否则 `graph.ainvoke` 会抛 "does not support async methods"）。同步实现仅适用于 `graph.invoke`。
   - 未启用 Postgres 时，`var/checkpoints.db` 明确为**单副本专用**；多副本临时形态是
     「粘性路由 + 每副本独立 var 卷」——只保会话连续性，重启即丢，且无法跨副本恢复 HITL 中断。
4. **短期记忆/限流共享**：`REDIS_URL` 必须指向所有副本共用的 Redis（compose 内已共用同一服务）。
5. **向量索引一致性**：FAISS 索引`不进镜像`（`.dockerignore` 排除 `vector_store/`），
   每个副本各自重建，扩容期 CPU/IO 抖动明显。多副本更推荐 `VECTOR_STORE_TYPE=milvus`
   （可选依赖：`pip install "heteroagent[milvus]"`），把索引放到共享服务。
6. **入口与就绪**：副本前面必须放反向代理做健康检查摘除，只把流量给 `/health/ready`=200 的副本；
   `/health/live`=200 只说明进程活着，不代表能服务。
6. **别开多 worker**：横向扩副本，纵向 `--workers` 会复制第 1/2/4 条的所有状态分裂问题。

---

## 5. 常见故障

| 症状 | 根因 | 处理 |
| --- | --- | --- |
| `/health/ready` 返回 **503**，`detail.graph=false` | `OPENAI_API_KEY` 未配置/为空，LLM 初始化失败（`api/main.py:219`） | 在 `.env` 填 key 后重启；容器确认 `--env-file .env` 生效。诊断只看 `/health/ready` 的 detail，别用 `/health`（它恒 200） |
| 聊天请求返回 503 `服务未就绪：LLM 未配置…` | 同上，`_require_graph()` 拒绝转发 | 同上 |
| `EmbeddingUnavailableError`（`memory/long_term.py:90`） | 向量化调用失败/维度与 `EMBEDDING_DIM` 不符/无 key；`ALLOW_EMBEDDING_FALLBACK=false` 时**禁止伪向量**，直接抛错（G7） | 核对 `EMBEDDING_MODEL` 与 `EMBEDDING_DIM` 是否匹配（换模型必须换维度）、`OPENAI_BASE_URL` 网关是否提供 embedding、网络可达性。**不建议**改成 `ALLOW_EMBEDDING_FALLBACK=true`：那只会把报错换成静默的低质检索结果 |
| **429** `请求过于频繁`（带 `Retry-After`） | 命中 `RATE_LIMIT_PER_MINUTE`（默认 60/调用方/分钟） | 按 `Retry-After` 退避；压测或批量导入再调该值。多副本时限流按 principal 计数，共用 Redis 才准确 |
| **413** `请求体过大` | body 超过 `MAX_BODY_BYTES`（默认 1000000） | 拆分请求或调该值；反向代理也要同步放开 `client_max_body_size` |
| **413** `消息长度超过上限` | 单条消息超 `MAX_MESSAGE_CHARS`（默认 8000） | 客户端截断/分段发送 |
| **401** `缺少或无效的 API Key` | `REQUIRE_AUTH=true` 但未带 `X-API-Key`，或 key 不在 `API_KEYS` 白名单 | `curl -H "X-API-Key: <key>"`；核对 `API_KEYS` 逗号分隔项无多余空格 |
| **400** `检测到疑似提示注入攻击` | `security` 注入检测命中（拦截不是解析失败） | 复核内容；确属误报再调整检测规则，别关掉整个检测 |
| 容器一直 `unhealthy` | 应用启动失败或 `start_period` 太短 | `docker compose logs app`；先看 `/health/ready` 的 detail 是哪个子项没起 |
| `docker build` 在 `COPY mcp/ ./mcp/` 失败 | Dockerfile 引用已改名的目录（实际为 `tooling/`） | 见第 2 节警告：改成 `COPY tooling/ ./tooling/` |
| `sqlite3.DatabaseError: database is locked` | 多个进程共享同一个 `var/checkpoints.db` | SQLite 不支持多进程共享写：回到单副本，或按第 4 节切 Postgres |
| Windows 下 `make` 不存在 | 未装 make | 用目标上方注释里的等价裸命令；`make` 仅要求命令行以 Tab 缩进，不要用空格重写 |

---

## 6. 安全清单（上线前逐条打勾）

- [ ] `API_KEYS` 设了强随机值（逗号分隔多调用方），`REQUIRE_AUTH=true`。默认 `false` 是开发姿态，
      公网裸奔等于把所有接口开放。
- [ ] `CORS_ORIGINS` 收紧到实际前端域名（默认含 `http://localhost:8000`）；不要用 `*`，
      `allow_credentials` 逻辑与 `*` 互斥（`api/main.py:296`）。
- [ ] **`.env` 不提交**：已在 `.gitignore`/`.dockerignore` 排除，仓库只留 `.env.example`。
      密钥泄漏后先轮换再排查，改历史不算处置完毕。
- [ ] `DEBUG=false`、`ENABLE_DEBUG_TOOL_ENDPOINTS=false`（调试端点可直调工具）。
- [ ] 容器以非 root 运行（Dockerfile 已固化 `appuser`），不要加 `--privileged` / `user: root`。
- [ ] Redis 不映射宿主端口（compose 已如此）；对外暴露时加 `requirepass` 并改用网络策略隔离。
- [ ] `MAX_BODY_BYTES` / `MAX_MESSAGE_CHARS` / `RATE_LIMIT_PER_MINUTE` 按真实流量设定，
      并在反向代理层同步 body 上限，否则大 payload 先打满应用内存。
- [ ] `APPROVAL_TTL_SECONDS`、`CHECKPOINT_RETENTION_DAYS` 与合规留存要求对齐；
      `var/` 里的检查点含会话内容，纳入备份与清理策略。
- [ ] `OTEL_CONSOLE_EXPORT=false`（生产 trace 走 `/health` 之外的 OTLP 端点，控制台导出会把内容打到 stdout）。
- [ ] 生产镜像 tag 固定版本（`heteroagent:0.3.0`），不用 `latest`；`jaegertracing/all-in-one:latest`
      仅限本地栈。

---

## 7. 与 CI 的关系

[.github/workflows/ci.yml](../.github/workflows/ci.yml)：

- `lint-test`（ubuntu-latest, Python 3.12, pip 缓存）：
  `ruff check . --exclude docs` → `pytest -q`，并显式 `OPENAI_API_KEY=""` /
  `REQUIRE_AUTH=false` / `OTEL_CONSOLE_EXPORT=false` 保证离线复现。
  本地 `make lint` + `make test` 与之等价（当前基线 188 passed）。
- `package`（needs lint-test，仅 push 到 main）：`docker/build-push-action` 且 `push: false`，
  只验证可构建性，不推 registry；受第 2 节的 `COPY mcp/` 阻塞影响。
- `concurrency` 按 workflow+ref 取消进行中的旧运行；`permissions: contents: read`。

尚未纳入 CI 的 T-P2-05 规划项（类型检查、评估回归门禁、依赖审计）：
`make eval` 可在本地先跑 `python -m evaluation.evaluator --no-llm` 产出检索指标基线。

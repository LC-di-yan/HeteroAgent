"""集中配置 — 唯一配置真相源（P0-08 / 4.6.1）。

所有环境变量在此收口；业务代码禁止直接 os.getenv。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── 应用 ──
    app_name: str = "heteroagent"
    # 离线演示模式：用脚本化假模型 + 关键字知识库跑通全链路（无需 API Key）
    # 生产必须保持 false；开启时启动日志会告警
    demo_mode: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    # ── LLM ──
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-5.1"
    light_model_name: str = ""          # 空 → 复用 model_name
    temperature: float = 0.0
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2

    # ── Embedding ──
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_timeout_seconds: float = 10.0
    # 禁止静默伪降级（G7）：False 时 embedding 不可用直接报错
    allow_embedding_fallback: bool = False

    # ── 检索 / 重排 ──
    rag_top_k: int = 5
    rag_rerank_top_k: int = 3
    rag_confidence_threshold: float = 0.30   # 由真实分数分布校准，见 T-P0-05
    rerank_enabled: bool = False
    # 工具调用：模型支持 bind_tools 时，业务动作走 tool_calls（T-P1-07）
    tool_calling_enabled: bool = True
    # 跨会话用户事实（T-P2-02）：工单号等确定性事实，写入 LangGraph Store
    context_store_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ── Redis / 短期记忆 ──
    redis_url: str = "redis://localhost:6379/0"
    short_term_ttl_seconds: int = 1800
    short_term_max_turns: int = 20
    max_context_tokens: int = 4000

    # ── 向量库 ──
    vector_store_type: Literal["faiss", "milvus"] = "faiss"
    faiss_index_path: str = "./vector_store/faiss_index"
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "heteroagent_docs"

    # ── 接口安全（G9）──
    api_keys: str = ""                  # 逗号分隔；为空且 require_auth=False 时放行
    require_auth: bool = False
    rate_limit_per_minute: int = 60
    # 多副本需共享配额时设 redis（默认进程内）
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    max_body_bytes: int = 1_000_000
    max_message_chars: int = 8000
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # ── 人工审批（T-P0-03）──
    approval_store: Literal["redis", "memory"] = "memory"
    approval_ttl_seconds: int = 86400

    # ── 合规 ──
    compliance_negation_window: int = 8

    # ── 安全护栏（T-P2-08）：规则之上叠加模型分类器 ──
    guardrail_enabled: bool = False
    guardrail_endpoint: str = ""          # 分类服务 URL（Llama Guard / Prompt Guard / 内部风控）
    guardrail_api_key: str = ""
    guardrail_timeout_seconds: float = 3.0
    guardrail_fail_closed: bool = False    # 分类器故障时：False=放行(可用性优先)，True=拦截

    # ── 检查点 ──
    checkpoint_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    checkpoint_path: str = "./var/checkpoints.db"
    # Postgres 检查点（多副本共享 HITL 状态）：需 pip install langgraph-checkpoint-postgres
    postgres_dsn: str = ""
    checkpoint_retention_days: int = 14

    # ── 可观测（G13：采样配置必须被读取）──
    otel_service_name: str = "heteroagent"
    otel_exporter_otlp_endpoint: str = ""
    otel_traces_sampler: str = "parentbased_always_on"
    otel_console_export: bool = False

    # ── Prometheus 文本端点：默认需鉴权，集群抓取可按需放开 ──
    metrics_public: bool = False

    # ── 调试端点（默认关闭）──
    enable_debug_tool_endpoints: bool = False

    # ── 派生属性 ──
    @property
    def effective_light_model(self) -> str:
        return self.light_model_name or self.model_name

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("rag_confidence_threshold")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("rag_confidence_threshold 必须在 [0,1]")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

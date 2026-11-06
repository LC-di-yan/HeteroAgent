"""检查点后端选择与失败语义。"""
from __future__ import annotations

import pytest

from agents.supervisor import _build_checkpointer
from config import Settings


def test_memory_backend():
    saver = _build_checkpointer(Settings(checkpoint_backend="memory"))
    assert type(saver).__name__ == "InMemorySaver"


def test_sqlite_backend(tmp_path):
    path = str(tmp_path / "cp.db")
    saver = _build_checkpointer(Settings(checkpoint_backend="sqlite", checkpoint_path=path))
    assert type(saver).__name__ == "SqliteSaver"


def test_postgres_requires_dsn():
    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        _build_checkpointer(Settings(checkpoint_backend="postgres", postgres_dsn=""))


def test_postgres_requires_optional_package():
    settings = Settings(checkpoint_backend="postgres", postgres_dsn="postgresql://u:p@localhost:5432/db")
    try:
        import langgraph.checkpoint.postgres  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    if installed:
        pytest.skip("本机已安装 langgraph-checkpoint-postgres，跳过缺包分支")
    with pytest.raises(RuntimeError, match="langgraph-checkpoint-postgres"):
        _build_checkpointer(settings)


def test_invalid_backend_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(checkpoint_backend="redis")

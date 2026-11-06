"""提交前检查脚本测试（只跑 ruff 的 fast 模式，避免嵌套跑全量测试）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_check_script_fast_mode_passes():
    import os

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "scripts/check.py", "--fast"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ruff 通过" in proc.stdout
    assert "已跳过测试" in proc.stdout


def test_hook_file_is_executable_by_git():
    hook = ROOT / ".githooks" / "pre-commit"
    assert hook.exists()
    content = hook.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh")
    assert "scripts/check.py" in content

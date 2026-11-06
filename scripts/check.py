"""提交前检查（跨平台）：ruff 静态检查 + pytest 离线测试。

用法：
    python scripts/check.py           # 完整检查（ruff + 全量测试）
    python scripts/check.py --fast    # 只跑 ruff（赶时间时）
    $env:SKIP_TESTS=1 ...             # 环境变量也可跳过测试

退出码：0 通过；1 失败（pre-commit 钩子据此阻断提交）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(cmd: list[str], label: str) -> bool:
    print(f"[check] {label}: {' '.join(cmd)}", flush=True)
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.perf_counter() - started
    if proc.returncode == 0:
        print(f"[check] {label} 通过（{elapsed:.1f}s）", flush=True)
        return True
    print(f"[check] {label} 失败（exit={proc.returncode}，{elapsed:.1f}s）", flush=True)
    return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    fast = "--fast" in args
    skip_tests = fast or os.getenv("SKIP_TESTS", "").lower() in {"1", "true", "yes"}

    steps: list[tuple[list[str], str]] = [
        ([sys.executable, "-m", "ruff", "check", ".", "--exclude", "docs", "--quiet"], "ruff"),
    ]
    if not skip_tests:
        steps.append(
            ([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"], "pytest")
        )
    else:
        print("[check] 已跳过测试（--fast 或 SKIP_TESTS=1）", flush=True)

    for cmd, label in steps:
        try:
            ok = _run(cmd, label)
        except FileNotFoundError:
            print(f"[check] 找不到 {label}，请先安装开发依赖", flush=True)
            return 1
        if not ok:
            return 1
    print("[check] 全部通过 ✅", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""把 static/ 打包成 GitHub Pages 上的静态演示（docs/demo/）。

前端本身零构建，这里只做两件事：
1. 复制 CSS / JS / 图标到 docs/demo/assets/（Pages 只发布 /docs，无法引用仓库根的 static/）；
2. 把 index.html 的资源路径改到 assets/，并在模块脚本之前注入 mock-backend.js。

用法：
    python scripts/build_demo.py          # 重新生成
    python scripts/build_demo.py --check  # 校验是否与 static/ 同步（CI / 测试用）
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
DEMO = ROOT / "docs" / "demo"
ASSETS = DEMO / "assets"


def _mapping() -> dict[Path, Path]:
    files = {STATIC / "css" / "app.css": ASSETS / "app.css"}
    for js in sorted((STATIC / "js").glob("*.js")):
        files[js] = ASSETS / "js" / js.name
    files[STATIC / "favicon.svg"] = ASSETS / "favicon.svg"
    files[STATIC / "manifest.webmanifest"] = ASSETS / "manifest.webmanifest"
    return files


def _render_index() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="css/app.css"', 'href="assets/app.css"')
    html = html.replace('href="favicon.svg"', 'href="assets/favicon.svg"')
    html = html.replace('href="manifest.webmanifest"', 'href="assets/manifest.webmanifest"')
    marker = '  <script type="module" src="js/app.js"></script>'
    if marker not in html:
        raise SystemExit("index.html 中的模块入口发生变化，请更新 build_demo.py")
    replacement = (
        '  <script src="mock-backend.js"></script>\n'
        '  <script type="module" src="assets/js/app.js"></script>'
    )
    return html.replace(marker, replacement)


def build(check: bool = False) -> int:
    problems: list[str] = []
    files = _mapping()
    index_text = _render_index()
    index_path = DEMO / "index.html"

    if check:
        for src, dst in files.items():
            if not dst.is_file() or dst.read_bytes() != src.read_bytes():
                problems.append(f"过期: {dst.relative_to(ROOT)}")
        if not index_path.is_file() or index_path.read_text(encoding="utf-8") != index_text:
            problems.append(f"过期: {index_path.relative_to(ROOT)}")
        for item in problems:
            print(item)
        print("demo 与 static 不同步，请运行 python scripts/build_demo.py" if problems else "demo 已同步")
        return 1 if problems else 0

    for src, dst in files.items():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(index_text, encoding="utf-8")
    (ROOT / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    print(f"已生成 {index_path.relative_to(ROOT)} 与 {len(files)} 个资源文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(build(check="--check" in sys.argv[1:]))

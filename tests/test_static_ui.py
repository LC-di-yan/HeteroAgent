"""静态前端契约测试（无需浏览器）。

目标：在不启动服务、不装 headless 浏览器的前提下，锁住
static/index.html 与 static/js/* 之间的接口，避免改一处漏一处。

覆盖：
- 入口语义与本地资源引用（不允许外链字体/脚本，离线也要能渲染）
- app.js 用到的 DOM id 必须真实存在
- 图标名必须在 icons.js 中注册（emoji 在 UI 中禁用）
- markdown 渲染不得使用 innerHTML 拼接模型输出
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX = STATIC_DIR / "index.html"
APP = STATIC_DIR / "js" / "app.js"
ICONS = STATIC_DIR / "js" / "icons.js"
MARKDOWN = STATIC_DIR / "js" / "markdown.js"
CSS = STATIC_DIR / "css" / "app.css"

EMOJI_RANGES = "[" + chr(0x1F000) + "-" + chr(0x1FAFF) + "\u2600-\u27BF\uFE0F]"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_index_has_semantics_and_entries() -> None:
    html = _read(INDEX)
    assert 'lang="zh-CN"' in html
    assert 'name="viewport"' in html
    assert 'type="module" src="js/app.js"' in html
    assert 'href="css/app.css"' in html
    assert 'rel="icon"' in html
    assert 'name="description"' in html
    # 无障碍骨架
    assert 'class="skip-link"' in html
    assert 'aria-live' in html
    assert 'role="log"' in html
    for tab in ("history", "approvals", "metrics", "settings"):
        assert f'data-tab="{tab}"' in html
        assert f'data-panel="{tab}"' in html


def test_no_external_assets() -> None:
    html = _read(INDEX)
    external = re.findall(r'(?:href|src)="(https?://[^"]+)"', html)
    assert external == [], f"不应依赖外部网络资源: {external}"
    assert "fonts.googleapis" not in html
    for module in (STATIC_DIR / "js").glob("*.js"):
        for url in re.findall(r"https?://[^\s\"')]+", _read(module)):
            allowed = ("localhost" in url or "127.0.0.1" in url or "w3.org" in url
                       or "example.com" in url)
            assert allowed, f"{module.name} 出现外部地址: {url}"


def test_local_assets_exist() -> None:
    html = _read(INDEX)
    for ref in re.findall(r'(?:href|src)="([^"]+)"', html):
        if ref.startswith(("http", "#", "data:", "mailto:")):
            continue
        assert (STATIC_DIR / ref).is_file(), f"缺少本地资源: {ref}"


def test_dom_ids_used_by_app_exist() -> None:
    html = _read(INDEX)
    ids = set(re.findall(r'id="([^"]+)"', html))
    app = _read(APP)
    used = set(re.findall(r"\$\('([^']+)'\)", app))
    used |= set(re.findall(r"getElementById\('([^']+)'\)", app))
    missing = sorted(name for name in used if name not in ids)
    assert missing == [], f"app.js 引用了不存在的 id: {missing}"


def test_icon_names_are_registered() -> None:
    icons_src = _read(ICONS)
    known = set(re.findall(r"^  ([A-Za-z0-9]+): \[", icons_src, re.M))
    known.add("stop")

    used: set[str] = set()
    for text in (_read(INDEX), _read(APP), icons_src):
        used |= set(re.findall(r'data-icon="([a-z0-9]+)"', text))
        used |= set(re.findall(r"\bicon\('([A-Za-z0-9]+)'", text))
        used |= set(re.findall(r"\biconButton\('([A-Za-z0-9]+)'", text))
        used |= set(re.findall(r"icon:\s*'([A-Za-z0-9]+)'", text))

    missing = sorted(name for name in used if name not in known)
    assert missing == [], f"未注册的图标: {missing}"


def test_no_emoji_in_ui_sources() -> None:
    pattern = re.compile(EMOJI_RANGES)
    for path in (INDEX, APP, ICONS, MARKDOWN, CSS):
        found = pattern.findall(_read(path))
        assert not found, f"{path.name} 仍包含 emoji: {found[:5]}"


def test_markdown_renderer_is_injection_safe() -> None:
    src = _read(MARKDOWN)
    assert ".innerHTML" not in src, "markdown 渲染不得使用 innerHTML 拼接模型输出"
    assert "textContent" in src
    assert "noopener" in src, "外链必须带 rel=noopener"


def test_app_avoids_blocking_dialogs() -> None:
    app = _read(APP)
    assert "alert(" not in app
    assert "confirm(" not in app


def test_theme_tokens_and_banned_patterns() -> None:
    css = _read(CSS)
    assert '[data-theme="dark"]' in css
    assert '[data-theme="light"]' in css
    assert "#000000" not in css.lower(), "禁止纯黑背景"
    assert "transition: all" not in css, "避免 all 过渡拖慢主题切换"
    assert "prefers-reduced-motion" in css


def test_demo_assets_in_sync() -> None:
    demo = STATIC_DIR.parent / "docs" / "demo"
    if not demo.is_dir():
        return
    pairs = [(STATIC_DIR / "css" / "app.css", demo / "assets" / "app.css")]
    for js in (STATIC_DIR / "js").glob("*.js"):
        pairs.append((js, demo / "assets" / "js" / js.name))
    for src, dst in pairs:
        assert dst.is_file(), f"缺少 demo 资源: {dst.relative_to(STATIC_DIR.parent)}"
        assert dst.read_bytes() == src.read_bytes(), (
            f"demo 资源过期: {dst.relative_to(STATIC_DIR.parent)}，请运行 python scripts/build_demo.py"
        )
    html = (demo / "index.html").read_text(encoding="utf-8")
    assert "mock-backend.js" in html
    assert 'src="assets/js/app.js"' in html
    assert "assets/app.css" in html


def test_static_modules_are_present() -> None:
    index = _read(INDEX)
    for module in ("app.js", "api.js", "icons.js", "markdown.js", "store.js"):
        assert (STATIC_DIR / "js" / module).is_file(), module
    assert "js/app.js" in index

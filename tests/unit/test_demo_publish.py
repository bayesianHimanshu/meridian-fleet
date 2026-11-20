"""The demo's release assets and page (docs/demo/README.md, "Publishing"): the manifest's
format, and a page that loads the video beside it and requests nothing else."""

import base64
import hashlib
import re
from pathlib import Path

PAGE = Path("docs/demo/index.html")
MANIFEST = Path("docs/demo/assets.sha256")
ASSETS = ("demo.mp4", "captions.vtt", "transcript.json")


def test_the_manifest_lists_the_three_assets_by_sha256_in_sha256sum_format() -> None:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    names = []
    for line in lines:
        m = re.fullmatch(r"([0-9a-f]{64})  ([a-z0-9.]+)", line)
        assert m, f"not a sha256sum line: {line!r}"
        names.append(m.group(2))
    assert tuple(names) == ASSETS
    assert MANIFEST.read_bytes().endswith(b"\n")


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_page_loads_only_the_video_beside_it() -> None:
    html = _page()
    refs = re.findall(r"""\b(?:src|href|poster|srcset|data)\s*=\s*["']([^"']*)["']""", html)
    assert refs == ["demo.mp4"]
    for tag in ("<link", "<img", "<iframe", "<object", "<embed", "<source", "<track", "<base"):
        assert tag not in html.lower(), tag


def test_the_page_script_and_style_make_no_request() -> None:
    html = _page()
    (style,) = re.findall(r"<style>([\s\S]*?)</style>", html)
    (script,) = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert "@import" not in style
    assert "url(" not in style
    for call in ("fetch(", "XMLHttpRequest", "import(", "WebSocket", "EventSource",
                 "sendBeacon", "http://", "https://", "new Image"):  # fmt: skip
        assert call not in script, call


def test_the_csp_allows_only_same_origin_media_and_the_page_s_own_inline_code() -> None:
    html = _page()
    (csp,) = re.findall(r'http-equiv="Content-Security-Policy" content="([^"]*)"', html)
    assert "default-src 'none'" in csp
    assert "media-src 'self' file:" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp

    def h(text: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"

    (style,) = re.findall(r"<style>([\s\S]*?)</style>", html)
    (script,) = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert f"style-src {h(style)}" in csp
    assert f"script-src {h(script)}" in csp

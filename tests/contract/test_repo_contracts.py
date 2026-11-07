"""Repository-wide rules that are cheap to check mechanically."""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "meridian"
EM_DASH = chr(0x2014)  # built at runtime so this file stays clean
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".hypothesis",
    "var",
    # Installed third-party packages and build output of web/ (git-ignored).
    "node_modules",
    "dist",
}


def _text_files() -> Iterator[tuple[Path, str]]:
    for path in sorted(REPO_ROOT.rglob("*")):
        rel = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        try:
            yield rel, path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary file


def test_scan_covers_required_locations() -> None:
    scanned = {rel for rel, _ in _text_files()}
    for required in ("README.md", "pyproject.toml", "config/default.toml", ".python-version"):
        assert Path(required) in scanned
    for top in ("src", "tests", "config"):
        assert any(rel.parts[0] == top for rel in scanned), top


def test_no_em_dash_anywhere() -> None:
    offenders = [
        f"{rel}:{lineno}"
        for rel, text in _text_files()
        for lineno, line in enumerate(text.splitlines(), start=1)
        if EM_DASH in line
    ]
    assert offenders == [], f"U+2014 found in: {offenders}"


def _imports(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                yield node.lineno, ".".join([*base, node.module] if node.module else base)
            elif node.module:
                yield node.lineno, node.module


@pytest.mark.parametrize("path", sorted((SRC / "domain").rglob("*.py")), ids=lambda p: p.name)
def test_domain_imports_nothing_from_meridian_outside_domain(path: Path) -> None:
    bad = [
        f"{path.name}:{lineno} imports {module}"
        for lineno, module in _imports(path)
        if (module == "meridian" or module.startswith("meridian."))
        and not (module == "meridian.domain" or module.startswith("meridian.domain."))
    ]
    assert bad == []


def test_wall_clock_is_read_only_in_system_clock() -> None:
    allowed = SRC / "platform" / "clock.py"
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path == allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"now", "utcnow", "today"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"datetime", "date"}
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []

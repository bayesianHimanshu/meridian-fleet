"""meridian-worker's import closure: no ledger store, faults, evals, ingest or network."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
FORBIDDEN = (
    "meridian.ledger.store",
    "meridian.faults",
    "meridian.evals",
    "meridian.data.ingest",
    "meridian.data.fetch",
    "meridian.platform.container",
    "meridian.sandbox",
)
NETWORK = ("httpx", "httpcore", "socket", "ssl", "urllib", "http", "requests", "asyncio")


def _module(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path) -> set[str]:
    name = _module(path)
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{a.name}" for a in node.names)
    return found


def _closure() -> dict[str, set[str]]:
    """Every meridian module reachable from meridian.worker, with its direct imports."""
    modules = {_module(p): p for p in SRC.rglob("*.py")}
    seen: dict[str, set[str]] = {}
    stack = ["meridian.worker"]
    while stack:
        current = stack.pop()
        if current in seen or current not in modules:
            continue
        direct = _imports(modules[current])
        seen[current] = direct
        stack.extend(m for m in direct if m in modules)
        # "from meridian.x import y" also loads the package meridian.x and its parents.
        stack.extend(p for m in direct for p in _parents(m) if p in modules)
    return seen


def _parents(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _hit(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(f"{p}.") for p in prefixes)


def test_worker_reaches_no_forbidden_meridian_module() -> None:
    closure = _closure()
    assert "meridian.agents.rbqm.kri_monitor" in closure  # the scan does follow imports
    reached = sorted(m for m in closure if _hit(m, FORBIDDEN))
    assert reached == []


@pytest.mark.parametrize("module", sorted(_closure()))
def test_no_meridian_module_on_the_worker_path_imports_the_network(module: str) -> None:
    direct = sorted(m for m in _closure()[module] if _hit(m, NETWORK))
    assert direct == [], f"{module} imports {direct}"

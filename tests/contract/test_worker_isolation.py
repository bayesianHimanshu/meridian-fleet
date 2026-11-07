"""The workers' import closures.

meridian-worker: no ledger store, faults, evals, ingest or network.
meridian-narrator-worker: none of those either, no study data and no detection code; it
reaches the network only through meridian.llm.client.
"""

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


def _closure(root: str = "meridian.worker") -> dict[str, set[str]]:
    """Every meridian module reachable from root, with its direct imports."""
    modules = {_module(p): p for p in SRC.rglob("*.py")}
    seen: dict[str, set[str]] = {}
    stack = [root]
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


NARRATOR_FORBIDDEN = (
    *FORBIDDEN,
    "meridian.data.study_store",
    "meridian.data.cut_writer",
    "meridian.agents",
    "meridian.worker",
)
NARRATOR_NETWORK_ONLY_IN = "meridian.llm.client"


def test_narrator_worker_reaches_no_forbidden_module() -> None:
    closure = _closure("meridian.narrator_worker")
    assert NARRATOR_NETWORK_ONLY_IN in closure
    reached = sorted(m for m in closure if _hit(m, NARRATOR_FORBIDDEN))
    assert reached == []


def test_narrator_worker_reaches_the_network_only_through_the_model_client() -> None:
    closure = _closure("meridian.narrator_worker")
    networked = sorted(m for m, direct in closure.items() if any(_hit(d, NETWORK) for d in direct))
    assert networked == [NARRATOR_NETWORK_ONLY_IN]


API_PACKAGES = ("fastapi", "uvicorn", "starlette", "meridian.api")


@pytest.mark.parametrize("root", ["meridian.worker", "meridian.narrator_worker"])
def test_workers_import_neither_fastapi_nor_uvicorn(root: str) -> None:
    closure = _closure(root)
    reached = sorted(
        m
        for m in closure
        if _hit(m, API_PACKAGES) or any(_hit(d, API_PACKAGES) for d in closure[m])
    )
    assert reached == [], f"{root} reaches {reached}"


def test_the_agent_image_export_excludes_the_api_group() -> None:
    """The Dockerfile's export (no dependency groups) lists neither package."""
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    dockerfile = (SRC.parent / "docker" / "agent.Dockerfile").read_text(encoding="utf-8")
    assert "uv export --frozen --no-default-groups --no-emit-project" in dockerfile
    out = subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--no-default-groups",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=SRC.parent,
    ).stdout
    names = {line.split("==")[0].strip().lower() for line in out.splitlines() if "==" in line}
    assert {"fastapi", "uvicorn", "starlette"}.isdisjoint(names)
    assert {"polars", "httpx"} <= names

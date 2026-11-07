"""Route handlers never touch the ledger or decide visibility themselves."""

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[2] / "src" / "meridian" / "api" / "routes"
FORBIDDEN = ("meridian.ledger", "meridian.api.visibility", "sqlite3")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found |= {module, *(f"{module}.{a.name}" for a in node.names)}
    return found


def test_route_handlers_import_neither_the_ledger_nor_visibility() -> None:
    files = sorted(ROUTES.glob("*.py"))
    assert any(f.name == "core.py" for f in files)
    for path in files:
        bad = sorted(
            m for m in _imports(path) if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)
        )
        assert bad == [], f"{path.name} imports {bad}"


def test_only_deps_and_repository_import_the_ledger_store() -> None:
    api = ROUTES.parent
    users = sorted(
        p.name
        for p in api.rglob("*.py")
        if any(m.startswith("meridian.ledger.store") for m in _imports(p))
    )
    assert users == ["deps.py", "repository.py"]

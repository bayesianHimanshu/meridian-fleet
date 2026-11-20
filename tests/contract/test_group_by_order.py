"""Every polars group_by must have a deterministic output order.

polars returns groups in an arbitrary order unless asked otherwise. When that order reaches
a computation (np.polyfit in step 3 was the lesson) or a file, results stop being
reproducible. Rule: each .group_by(...) either passes maintain_order=True or is followed by
a .sort(...) in the same method chain.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FILES = sorted([*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "scripts").rglob("*.py")])


def _violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    bad: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "group_by"
        ):
            continue
        if any(
            k.arg == "maintain_order"
            and isinstance(k.value, ast.Constant)
            and k.value.value is True
            for k in node.keywords
        ):
            continue
        sorted_later = False
        current: ast.AST = node
        while (parent := parents.get(current)) is not None:
            if isinstance(parent, ast.Attribute) and parent.value is current:
                if parent.attr == "sort":
                    sorted_later = True
                    break
            elif not (isinstance(parent, ast.Call) and parent.func is current):
                break
            current = parent
        if not sorted_later:
            bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_group_by_output_order_is_deterministic(path: Path) -> None:
    assert _violations(path) == [], f"{path.relative_to(REPO_ROOT)} lines {_violations(path)}"


def test_checker_catches_an_unordered_group_by(tmp_path: Path) -> None:
    sample = tmp_path / "x.py"
    sample.write_text(
        "a = df.group_by('k').agg(x)\n"
        "b = df.group_by('k').agg(x).sort('k')\n"
        "c = df.group_by('k', maintain_order=True).len()\n"
        "d = dict(df.group_by('k').len().iter_rows())\n"
    )
    assert _violations(sample) == [1, 4]

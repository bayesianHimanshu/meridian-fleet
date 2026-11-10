"""scripts/check.sh and the pre-commit hook stop on a failing check.

The tools are replaced by stubs on PATH that record their arguments, so this runs the
real script without running ruff, mypy, pytest or npm.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "check.sh"
HOOK = ROOT / ".githooks" / "pre-commit"

STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "{log}"
if [[ "$(basename "$0") $*" == *"{fail}"* ]]; then exit 1; fi
if [[ "$(basename "$0")" == git && "$1" == rev-parse ]]; then echo "{root}"; fi
exit 0
"""


def _stubs(tmp_path: Path, fail: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("uv", "npm", "git"):
        stub = bin_dir / tool
        stub.write_text(STUB.format(log=log, fail=fail or "\x00never", root=ROOT))
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    return env, log


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False, cwd=ROOT)


def test_every_check_passing_exits_zero(tmp_path: Path) -> None:
    env, log = _stubs(tmp_path, fail="")
    result = _run([str(CHECK)], env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert "uv run ruff format --check" in calls
    assert "uv run ruff check" in calls
    assert "uv run mypy" in calls
    assert "uv run pytest -q -m not slow and not openshell" in calls
    assert "npm --prefix web run --silent check:api" in calls
    assert "check: all passed" in result.stdout


@pytest.mark.parametrize(
    "fail",
    [
        "ruff format --check",
        "ruff check",
        "uv run mypy",
        "uv run pytest",
        "meridian policy render",
        "run --silent typecheck",
        "run --silent lint",
        "run --silent test",
        "check:api",
    ],
)
def test_a_failing_check_exits_non_zero(tmp_path: Path, fail: str) -> None:
    env, log = _stubs(tmp_path, fail=fail)
    result = _run([str(CHECK)], env)
    assert result.returncode != 0
    assert "check: FAILED" in result.stderr
    assert "all passed" not in result.stdout
    # it stops at the failing check: nothing after it ran
    assert fail in log.read_text().splitlines()[-1]


def test_no_web_skips_the_web_checks(tmp_path: Path) -> None:
    env, log = _stubs(tmp_path, fail="npm")
    result = _run([str(CHECK), "--no-web"], env)
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("npm") for c in log.read_text().splitlines())


def test_the_hook_runs_the_check_and_fails_with_it(tmp_path: Path) -> None:
    env, _ = _stubs(tmp_path, fail="uv run mypy")
    result = _run([str(HOOK)], env)
    assert result.returncode != 0
    assert "check: FAILED: mypy --strict" in result.stderr


def test_the_hook_passes_when_the_check_passes(tmp_path: Path) -> None:
    env, _ = _stubs(tmp_path, fail="")
    assert _run([str(HOOK)], env).returncode == 0

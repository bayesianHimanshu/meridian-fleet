import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian.cli import app

runner = CliRunner()


@pytest.fixture
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("MERIDIAN_LEDGER_PATH", str(path))
    return path


def test_cli_end_to_end(ledger_env: Path) -> None:
    assert runner.invoke(app, ["ledger", "init"]).exit_code == 0
    first = runner.invoke(app, ["ledger", "append-demo"])
    second = runner.invoke(app, ["ledger", "append-demo"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "DSMB-0002" in second.stdout

    full = runner.invoke(app, ["ledger", "show", "--role", "unblinded_statistician"])
    lines = [json.loads(line) for line in full.stdout.splitlines()]
    assert [line["seq"] for line in lines] == [1, 2, 3, 4]
    assert all("payload" in line for line in lines)

    blinded = runner.invoke(app, ["ledger", "show", "--role", "clinical_ops_lead"])
    redacted = [json.loads(line) for line in blinded.stdout.splitlines()]
    assert [line.get("marker") for line in redacted] == [None, "restricted", None, "restricted"]

    since = runner.invoke(app, ["ledger", "show", "--since", "3"])
    assert [json.loads(line)["seq"] for line in since.stdout.splitlines()] == [4]

    verified = runner.invoke(app, ["ledger", "verify"])
    assert verified.exit_code == 0
    assert json.loads(verified.stdout)["ok"] is True


def test_cli_verify_on_missing_ledger_fails(ledger_env: Path) -> None:
    result = runner.invoke(app, ["ledger", "verify"])
    assert result.exit_code == 1
    assert not ledger_env.exists()

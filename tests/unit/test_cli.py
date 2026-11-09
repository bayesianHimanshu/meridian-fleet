import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian.agents.base import FindingDraft
from meridian.cli import app
from meridian.domain.events import Event

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


# ---- the runner's default executor


@dataclass(frozen=True)
class _Outcome:
    drafts: tuple[FindingDraft, ...] = ()
    image_digest: str | None = "sha256:" + "0" * 64
    policy_sha256: str | None = None
    inputs_manifest_sha256: str | None = None
    policy_file: str | None = None
    policy_file_sha256: str | None = None


class _FakeOpenShell:
    name = "openshell"

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def run(self, agent_id: str, trigger: Event, cut_id: str) -> _Outcome:
        self.runs.append((agent_id, cut_id))
        return _Outcome()

    def drain(self) -> None:
        return None


def _pending_dc_base(ledger_env: Path) -> None:
    from meridian.data.datacut import record_data_cut
    from meridian.ledger.store import SqliteLedger
    from meridian.platform.clock import FixedClock
    from meridian.platform.config import Settings
    from tests.support import T0

    settings = Settings()
    with SqliteLedger(
        path=ledger_env, clock=FixedClock(T0), finding_id_prefixes=settings.finding_id_prefixes
    ) as ledger:
        ledger.initialize()
        record_data_cut(
            ledger,
            clock=FixedClock(T0),
            actor="system.ingest",
            study_id=settings.study_id,
            cut_id="DC-BASE",
            path=Path("curated/MRD-201/DC-BASE"),
        )


def test_a_plain_agents_run_uses_openshell(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.platform.container import Container

    fake = _FakeOpenShell()

    def no_inprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("the in-process executor was built without being asked for")

    monkeypatch.setattr(Container, "openshell_executor", lambda self, log: fake)
    monkeypatch.setattr(Container, "inprocess_executor", no_inprocess)
    _pending_dc_base(ledger_env)
    result = runner.invoke(app, ["agents", "run"])
    assert result.exit_code == 0, result.output
    assert fake.runs
    assert {cut for _, cut in fake.runs} == {"DC-BASE"}
    shown = runner.invoke(app, ["ledger", "show", "--role", "qa_auditor"])
    woke = [
        json.loads(line)
        for line in shown.stdout.splitlines()
        if json.loads(line)["payload"]["type"] == "actor_woke"
    ]
    assert woke
    assert {w["payload"]["executor"] for w in woke} == {"openshell"}


def test_agents_run_in_process_on_dc_base_is_refused(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_dc_base(ledger_env)
    result = runner.invoke(app, ["agents", "run", "--executor", "inprocess"])
    assert result.exit_code == 2
    assert "operational cut(s) DC-BASE" in result.output

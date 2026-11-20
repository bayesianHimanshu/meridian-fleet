import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian.agents.base import FindingDraft
from meridian.agents.runner import RunPlan
from meridian.cli import app
from meridian.domain.events import Event

runner = CliRunner()


@pytest.fixture
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("MERIDIAN_LEDGER_PATH", str(path))
    return path


def _append_cut_and_restricted_finding(path: Path, n: int) -> None:
    """A DataCut and one restricted finding (what the retired `ledger append-demo` wrote)."""
    from meridian.domain.enums import FindingKind, Role, Severity, Visibility
    from meridian.domain.events import DataCut, EventDraft, FindingRaised
    from meridian.domain.findings import Evidence, Finding
    from meridian.platform.clock import FixedClock
    from tests.support import T0, make_ledger

    with make_ledger(path, FixedClock(T0)) as ledger:
        cut = ledger.append(
            EventDraft(occurred_at=T0, actor="system.ingest", study_id="MER-101",
                       visibility=Visibility.BLINDED,
                       payload=DataCut(cut_id=f"CUT-{n:04d}", study_id="MER-101",
                                       path=f"cut-{n:04d}"))
        )  # fmt: skip
        finding = Finding(
            kind=FindingKind.QTL_BREACH, agent_id="unblinded.efficacy_watch",
            agent_version="0.1.0", study_id="MER-101", site_id="SITE-007",
            severity=Severity.HIGH, title="Arm-level dropout above QTL", summary="Test.",
            evidence=(Evidence(source=f"data_cut:{cut.seq}", selector="arm=B",
                               statistic="dropout_rate", value=0.21, threshold=0.15),),
            visibility=Visibility.RESTRICTED,
            visible_to=frozenset({Role.UNBLINDED_STATISTICIAN}), score=0.87,
        )  # fmt: skip
        ledger.append(
            EventDraft(occurred_at=T0, actor="unblinded.efficacy_watch", study_id="MER-101",
                       visibility=Visibility.RESTRICTED,
                       payload=FindingRaised(finding=finding, dedup_key=f"demo:{cut.seq}"))
        )  # fmt: skip


def test_cli_end_to_end(ledger_env: Path) -> None:
    assert runner.invoke(app, ["ledger", "init"]).exit_code == 0
    _append_cut_and_restricted_finding(ledger_env, 1)
    _append_cut_and_restricted_finding(ledger_env, 2)

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
    substrate: None = None


class _FakeOpenShell:
    name = "openshell"

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        return RunPlan(self.name)

    def capacity(self, executor: str) -> int:
        return 1

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> _Outcome:
        self.runs.append((agent_id, cut_id))
        return _Outcome()

    def drain(self) -> None:
        return None


class _FakeSubstrate(_FakeOpenShell):
    name = "substrate"


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


def test_a_plain_agents_run_uses_each_zones_executor(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[runtime] executor zoned: every enabled agent's policy is in [runtime.executors] as
    substrate, so DC-BASE runs go to substrate; nothing runs in process."""
    from meridian.platform.container import Container

    openshell, substrate = _FakeOpenShell(), _FakeSubstrate()

    def no_inprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("the in-process executor was built without being asked for")

    monkeypatch.setattr(Container, "openshell_executor", lambda self, log: openshell)
    monkeypatch.setattr(Container, "substrate_executor", lambda self, log: substrate)
    monkeypatch.setattr(Container, "inprocess_executor", no_inprocess)
    _pending_dc_base(ledger_env)
    result = runner.invoke(app, ["agents", "run"])
    assert result.exit_code == 0, result.output
    assert openshell.runs == []
    assert {cut for _, cut in substrate.runs} == {"DC-BASE"}
    assert {a for a, _ in substrate.runs} == {
        "rbqm.csm",
        "rbqm.kri_monitor",
        "rbqm.qtl_detector",
        "safety.sae_reconciler",
        "quality.tmf_completeness",
    }
    shown = runner.invoke(app, ["ledger", "show", "--role", "qa_auditor"])
    woke = [
        json.loads(line)
        for line in shown.stdout.splitlines()
        if json.loads(line)["payload"]["type"] == "actor_woke"
    ]
    assert woke
    assert {w["payload"]["executor"] for w in woke} == {"substrate"}


def test_agents_run_with_executor_openshell_sends_everything_to_openshell(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.platform.container import Container

    fake = _FakeOpenShell()
    monkeypatch.setattr(Container, "openshell_executor", lambda self, log: fake)
    _pending_dc_base(ledger_env)
    result = runner.invoke(app, ["agents", "run", "--executor", "openshell"])
    assert result.exit_code == 0, result.output
    assert {cut for _, cut in fake.runs} == {"DC-BASE"}


def test_agents_run_in_process_on_dc_base_is_refused(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_dc_base(ledger_env)
    result = runner.invoke(app, ["agents", "run", "--executor", "inprocess"])
    assert result.exit_code == 2
    assert "operational cut(s) DC-BASE" in result.output


def test_a_rerun_of_every_cut_must_be_asked_for_explicitly(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.platform.container import Container

    openshell, substrate = _FakeOpenShell(), _FakeSubstrate()
    monkeypatch.setattr(Container, "openshell_executor", lambda self, log: openshell)
    monkeypatch.setattr(Container, "substrate_executor", lambda self, log: substrate)
    _pending_dc_base(ledger_env)
    assert runner.invoke(app, ["agents", "run"]).exit_code == 0
    first = len(substrate.runs)
    assert runner.invoke(app, ["agents", "run"]).exit_code == 0
    assert len(substrate.runs) == first  # nothing pending
    assert runner.invoke(app, ["agents", "run", "--rerun"]).exit_code == 2
    assert runner.invoke(app, ["agents", "run", "--all-cuts"]).exit_code == 2
    again = runner.invoke(app, ["agents", "run", "--rerun", "--all-cuts"])
    assert again.exit_code == 0, again.output
    assert len(substrate.runs) == 2 * first
    assert "runs per executor: substrate" in again.output


# ---- api serve --fixed-now (the demo): refused without --demo-read-only


def test_fixed_now_is_refused_without_the_read_only_demo_mode(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    from meridian.platform.clock import FixedClock

    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(app))
    assert runner.invoke(app, ["ledger", "init"]).exit_code == 0
    alone = runner.invoke(app, ["api", "serve", "--fixed-now", "2026-11-20T12:00:00+00:00"])
    assert alone.exit_code == 2
    assert "--fixed-now is for the read-only demo mode only" in alone.output
    assert started == []  # nothing served
    both = runner.invoke(
        app,
        ["api", "serve", "--demo-read-only", str(tmp_path / "r.jsonl"),
         "--fixed-now", "2026-11-20T12:00:00+00:00"],
    )  # fmt: skip
    assert both.exit_code == 0, both.output
    (served,) = started
    clock = served.state.context.clock  # type: ignore[attr-defined]
    assert isinstance(clock, FixedClock)
    assert clock.now().isoformat() == "2026-11-20T12:00:00+00:00"

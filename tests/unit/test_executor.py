"""OpenShellExecutor against a fake openshell client: staging, cleanup, failures."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.agents.runner import AgentRunner
from meridian.data.datacut import record_data_cut
from meridian.data.study_store import ParquetStudyReader
from meridian.domain.enums import EventType
from meridian.domain.events import ActorSuspended, ActorWoke, Event
from meridian.ledger.store import SqliteLedger
from meridian.platform.agent_settings import QtlDetectorConfig
from meridian.platform.clock import FixedClock
from meridian.platform.config import AgentsSettings
from meridian.sandbox import policies
from meridian.sandbox.executor import ExecutionError, OpenShellExecutor
from meridian.sandbox.openshell import ExecOutput, ExecTimeoutError, OpenShellError
from meridian.sandbox.staging import StagingError, assert_clean_stage, stage_cut
from tests.rbqm_support import NOW, STUDY
from tests.support import make_ledger
from tests.worker_support import cut_trigger, tiny_cut

DIGEST = "sha256:" + "c" * 64
AGENTS = AgentsSettings(qtl_detector=QtlDetectorConfig(discontinuation_limit=0.617))
REPO = Path(__file__).resolve().parents[2]


@dataclass
class FakeOpenShellClient:
    """Records every call. exec returns `output` or raises `exec_error`."""

    output: ExecOutput | None = None
    exec_error: Exception | None = None
    effective: str = policies.expected_effective(policies.POLICIES["rbqm"])
    delete_error: Exception | None = None
    live: set[str] = field(default_factory=set)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    uploaded: list[set[str]] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    policies: list[Path] = field(default_factory=list)

    def create(
        self, name: str, image: str, policy: Path, uploads: Sequence[tuple[Path, str]]
    ) -> None:
        self.created.append(name)
        self.live.add(name)
        self.policies.append(policy)
        for local, remote in uploads:
            assert remote == "/tmp"
            self.uploaded.append({p.relative_to(local.parent).as_posix() for p in local.rglob("*")})
            self.manifests.append((local / "inputs_manifest.json").read_text())

    def exec(self, name: str, argv: Sequence[str], timeout_seconds: int) -> ExecOutput:
        assert name in self.live
        assert argv[:2] == ["/opt/meridian/venv/bin/meridian-worker", "run"]
        if self.exec_error is not None:
            raise self.exec_error
        assert self.output is not None
        return self.output

    def policy(self, name: str) -> str:
        return self.effective

    def delete(self, name: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(name)
        self.live.discard(name)

    def names(self) -> list[str]:
        return sorted(self.live)


@pytest.fixture
def cut_dir(tmp_path: Path) -> Path:
    cut = tiny_cut(tmp_path / "curated")
    # Things that must never leave the host, sitting right next to the cut.
    (cut / "ground_truth.json").write_text('{"faults": []}')
    return cut


def _expected(cut_dir: Path, agent: str = "rbqm.csm") -> str:
    registered = build_registry(AGENTS).get(agent)
    reader = ParquetStudyReader(cut_dir.parent, cut_dir.name)
    return drafts_document(
        registered, run_agent(registered, cut_trigger(), reader, structlog.get_logger())
    )


def _executor(
    client: FakeOpenShellClient, cut_dir: Path, *, keep_failed: bool = False
) -> OpenShellExecutor:
    return OpenShellExecutor(
        client=client,
        registry=build_registry(AGENTS),
        study_root=cut_dir.parent,
        image="meridian-agent:test",
        image_digest=DIGEST,
        policies_dir=REPO / "policies",
        exec_timeout_seconds=5,
        keep_failed=keep_failed,
        log=structlog.get_logger(),
    )


def test_success_returns_the_drafts_records_policy_and_deletes(cut_dir: Path) -> None:
    client = FakeOpenShellClient(output=ExecOutput(0, _expected(cut_dir), "log line\n"))
    result = _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert result.executor == "openshell"
    assert result.image_digest == DIGEST
    assert result.policy_sha256 == policies.sha256_text(client.effective)
    assert drafts_document(build_registry(AGENTS).get("rbqm.csm"), result.drafts) == _expected(
        cut_dir
    )
    assert result.stderr_tail == "log line\n"
    (manifest,) = client.manifests
    assert result.inputs_manifest_sha256 == hashlib.sha256(manifest.encode()).hexdigest()
    assert client.deleted == client.created
    assert client.live == set()
    assert client.policies == [REPO / "policies" / "rbqm.yaml"]


def test_staging_holds_only_blinded_ops_and_the_trigger(cut_dir: Path) -> None:
    assert (cut_dir / "unblinded").is_dir()
    assert (cut_dir / "manifest.json").is_file()
    client = FakeOpenShellClient(output=ExecOutput(0, _expected(cut_dir), ""))
    _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    (seen,) = client.uploaded
    tops = {p.split("/")[1] for p in seen if p.count("/") >= 1}
    assert tops == {"blinded", "ops", "trigger.json", "inputs_manifest.json"}
    assert not [
        p for p in seen if "unblinded" in p or "ground_truth" in p or p.endswith("/manifest.json")
    ]
    assert all(p.endswith(".parquet") for p in seen if p.count("/") == 2)


def test_assert_clean_stage_refuses_anything_extra(tmp_path: Path, cut_dir: Path) -> None:
    stage = stage_cut(cut_dir, cut_trigger(), tmp_path / "s")
    (stage / "unblinded").mkdir()
    with pytest.raises(StagingError):
        assert_clean_stage(stage)
    (stage / "unblinded").rmdir()
    (stage / "ops" / "notes.json").write_text("{}")
    with pytest.raises(StagingError):
        assert_clean_stage(stage)


def test_a_symlink_in_the_cut_is_refused(tmp_path: Path, cut_dir: Path) -> None:
    (cut_dir / "blinded" / "rand.parquet").symlink_to(
        next((cut_dir / "unblinded").glob("*.parquet"))
    )
    with pytest.raises(StagingError, match="unexpected entry"):
        stage_cut(cut_dir, cut_trigger(), tmp_path / "s")


@pytest.mark.parametrize(
    ("output", "error", "match"),
    [
        (ExecOutput(4, "", "worker.agent_error"), None, "exited 4"),
        (ExecOutput(3, "", "worker.data_error"), None, "exited 3"),
        (None, ExecTimeoutError("openshell sandbox exec timed out"), "timed out"),
        (ExecOutput(0, "not json", ""), None, "not a drafts document"),
        (ExecOutput(0, '{"agent_id": "rbqm.csm"}', ""), None, "not a drafts document"),
    ],
    ids=["agent-error", "data-error", "timeout", "garbage", "incomplete"],
)
def test_every_failure_raises_and_still_deletes(
    cut_dir: Path, output: ExecOutput | None, error: Exception | None, match: str
) -> None:
    client = FakeOpenShellClient(output=output, exec_error=error)
    with pytest.raises(ExecutionError, match=match):
        _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert len(client.created) == 1
    assert client.deleted == client.created
    assert client.live == set()


def test_wrong_agent_in_the_document_is_a_failure(cut_dir: Path) -> None:
    other = _expected(cut_dir, "rbqm.qtl_detector")
    client = FakeOpenShellClient(output=ExecOutput(0, other, ""))
    with pytest.raises(ExecutionError, match=r"expected rbqm\.csm"):
        _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert client.live == set()


def test_a_different_effective_policy_is_a_failure(cut_dir: Path) -> None:
    client = FakeOpenShellClient(
        output=ExecOutput(0, _expected(cut_dir), ""),
        effective=policies.expected_effective(policies.POLICIES["rbqm"]).replace(
            "    - /tmp", "    - /tmp\n    - /home"
        ),
    )
    with pytest.raises(ExecutionError, match="differs"):
        _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert client.live == set()


def test_create_failure_still_deletes(cut_dir: Path) -> None:
    """A failed upload leaves the sandbox behind (CLI 0.1.7), so delete it anyway."""

    @dataclass
    class FailingCreate(FakeOpenShellClient):
        def create(
            self, name: str, image: str, policy: Path, uploads: Sequence[tuple[Path, str]]
        ) -> None:
            super().create(name, image, policy, uploads)
            raise OpenShellError("sandbox upload failed")

    client = FailingCreate()
    with pytest.raises(ExecutionError, match="upload failed"):
        _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert client.deleted == client.created


def test_keep_failed_keeps_the_sandbox_and_logs_its_name(cut_dir: Path) -> None:
    client = FakeOpenShellClient(output=ExecOutput(4, "", "boom"))
    with capture_logs() as logs, pytest.raises(ExecutionError):
        _executor(client, cut_dir, keep_failed=True).run("rbqm.csm", cut_trigger(), "DC-TINY")
    (name,) = client.created
    assert client.deleted == []
    assert client.live == {name}
    kept = [e for e in logs if e["event"] == "sandbox.kept_for_debugging"]
    assert kept
    assert kept[0]["sandbox"] == name


def test_keep_failed_still_deletes_after_success(cut_dir: Path) -> None:
    client = FakeOpenShellClient(output=ExecOutput(0, _expected(cut_dir), ""))
    _executor(client, cut_dir, keep_failed=True).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert client.live == set()


def test_delete_failure_is_logged_not_raised(cut_dir: Path) -> None:
    client = FakeOpenShellClient(
        output=ExecOutput(0, _expected(cut_dir), ""), delete_error=OpenShellError("gateway gone")
    )
    with capture_logs() as logs:
        result = _executor(client, cut_dir).run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert result.drafts is not None
    assert [e["sandbox"] for e in logs if e["event"] == "sandbox.delete_failed"] == client.created


def test_sandbox_names_fit_the_gateway_limit() -> None:
    from meridian.sandbox.executor import MAX_SANDBOX_NAME, sandbox_name

    for agent in ("rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm"):
        for seq in (1, 275, 46655, 10**9):
            name = sandbox_name(agent, seq)
            assert len(name) <= MAX_SANDBOX_NAME, name
            assert name.startswith("mer-")
    assert sandbox_name("rbqm.kri_monitor", 275).startswith("mer-km-7n-")


def _ledger_with_cut(tmp_path: Path, cut_dir: Path) -> tuple[SqliteLedger, Event]:
    ledger = make_ledger(tmp_path / "l.sqlite3", FixedClock(NOW))
    event, _ = record_data_cut(
        ledger,
        clock=FixedClock(NOW),
        actor="system.ingest",
        study_id=STUDY,
        cut_id="DC-TINY",
        path=cut_dir,
    )
    return ledger, event


def test_runner_records_the_executor_and_retries_a_failed_run(
    tmp_path: Path, cut_dir: Path
) -> None:
    ledger, cut = _ledger_with_cut(tmp_path, cut_dir)
    registry = build_registry(AGENTS.model_copy(update={"enabled": ("rbqm.csm",)}))
    client = FakeOpenShellClient(output=ExecOutput(4, "", "boom"))
    executor = _executor(client, cut_dir)
    runner = AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=executor,
        clock=FixedClock(NOW),
        study_id=STUDY,
        log=structlog.get_logger(),
    )
    first = runner.run_pending()
    assert [f[0] for f in first.failed] == ["rbqm.csm"]
    assert ledger.read(event_types=[EventType.ACTOR_SUSPENDED]) == []

    client.output = ExecOutput(0, _expected_for(cut_dir, cut), "")
    second = runner.run_pending()
    assert second.completed == [("rbqm.csm", cut.seq)]
    woke = [
        e.payload for e in ledger.read(event_types=[EventType.ACTOR_WOKE]) if isinstance(e, Event)
    ]
    assert all(isinstance(w, ActorWoke) and w.executor == "openshell" for w in woke)
    (done,) = [
        e.payload
        for e in ledger.read(event_types=[EventType.ACTOR_SUSPENDED])
        if isinstance(e, Event)
    ]
    assert isinstance(done, ActorSuspended)
    assert done.executor == "openshell"
    assert done.image_digest == DIGEST
    assert done.policy_sha256 == policies.sha256_text(client.effective)
    assert client.live == set()
    ledger.close()


def _expected_for(cut_dir: Path, trigger: Event) -> str:
    registered = build_registry(AGENTS).get("rbqm.csm")
    reader = ParquetStudyReader(cut_dir.parent, cut_dir.name)
    return drafts_document(
        registered, run_agent(registered, trigger, reader, structlog.get_logger())
    )

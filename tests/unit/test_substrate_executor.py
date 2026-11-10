"""SubstrateExecutor against a fake kubectl ate client, with the actor's side in process.

The fake's proxy hands the request to meridian.worker_serve.run_once, the code an actor
runs, so a run here goes through the real archive, unpack, inputs manifest check and
worker; only the cluster is missing. Proof C4 of phase 4a step 2 is
test_an_actor_receives_only_its_zones_folders: for every substrate policy, the archive
holds exactly the policy's areas, the actor accepts nothing else, and the template's
--areas names the same set.
"""

import contextlib
import hashlib
import io
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.audit.extract import write_extract
from meridian.data.study_store import ParquetStudyReader
from meridian.domain.events import AuditReviewRequested, Event
from meridian.platform.agent_settings import QtlDetectorConfig
from meridian.platform.clock import FixedClock
from meridian.platform.config import AgentsSettings, SubstrateSettings
from meridian.platform.tmf_reference import load_tmf_reference
from meridian.sandbox.executor import ExecutionError, stage_run
from meridian.sandbox.policies import ALL_POLICIES
from meridian.sandbox.substrate import NotFoundError
from meridian.sandbox.substrate_executor import (
    SubstrateExecutor,
    ZoneTemplate,
    archive,
    archive_members,
    foreign_snapshots,
)
from meridian.sandbox.substrate_templates import SUBSTRATE_POLICIES, areas_of, zone_command
from meridian.sandbox.tracking import SandboxTracker
from meridian.tmf.generate import ensure_clean_snapshot
from meridian.worker_serve import AGENT_HEADER, SHA_HEADER, ArchiveError, _State, run_once, unpack
from tests.rbqm_support import NOW, STUDY
from tests.tmf_support import tmf_base
from tests.worker_support import cut_trigger, tiny_cut

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "default.toml"
AGENTS = AgentsSettings(qtl_detector=QtlDetectorConfig(discontinuation_limit=0.617))
GOLDEN = "f52e7bb5-6906-4f56-87df-c546b858d8b3"
IMAGE = "localhost:5001/meridian-agent@sha256:" + "c" * 64


def _template(policy: str) -> ZoneTemplate:
    return ZoneTemplate(
        policy=policy,
        name=f"{policy.replace('.', '-').replace('_', '-')}-cccccccccccc",
        sha256="7" * 64,
        file=f"deploy/substrate/templates/{policy}.yaml",
        file_sha256="6" * 64,
        image=IMAGE,
        golden_tag="aa7b3ef7-873c-4f20-8e0b-9e3957f78f3f",
        golden_snapshot=GOLDEN,
    )


@dataclass
class FakeActorClient:
    """Actors in a dict. proxy yields an address the fake post below understands."""

    templates: dict[str, str]  # template name to its policy
    snapshots: list[dict[str, Any]] = field(
        default_factory=lambda: [{"uuid": GOLDEN, "owner": "SNAPSHOT_OWNER_TAG"}]
    )
    live: dict[str, str] = field(default_factory=dict)  # actor to template
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    received: list[bytes] = field(default_factory=list)
    create_error: Exception | None = None

    def get_template(self, name: str) -> dict[str, Any]:
        return {}

    def get_tag(self, name: str) -> dict[str, Any]:
        return {}

    def get_actor(self, name: str) -> dict[str, Any] | None:
        if name not in self.live:
            return None
        return {
            "metadata": {"name": name, "uid": "uid-" + name},
            "status": {
                "snapshots": self.snapshots,
                "workerAssignment": {
                    "workerPod": "meridian-abc",
                    "sandboxRuntime": {"sandboxClass": "gvisor"},
                },
            },
        }

    def actors(self) -> list[dict[str, Any]]:
        return [{"metadata": {"name": n}} for n in self.live]

    def create_actor(self, name: str, template: str) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(name)
        self.live[name] = template
        return {}

    def resume_actor(self, name: str) -> dict[str, Any]:
        assert name in self.live
        return {}

    def delete_actor(self, name: str) -> None:
        self.deleted.append(name)
        if self.live.pop(name, None) is None:
            raise NotFoundError(name)

    @contextlib.contextmanager
    def proxy(self, name: str, port: int) -> Iterator[str]:
        assert port == 8080
        yield f"actor-{name}:{port}"

    def post(
        self, url: str, *, content: bytes, headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        """The actor's side: meridian-worker serve's run_once with the template's --areas."""
        actor = url.removeprefix("http://actor-").split(":", 1)[0]
        policy = self.templates[self.live[actor]]
        self.received.append(content)
        state = _State(areas_of(policy), CONFIG, 512 * 1024 * 1024)
        assert state.claim()
        try:
            body = run_once(state, headers[AGENT_HEADER], content, headers[SHA_HEADER])
        except ArchiveError as exc:
            return httpx.Response(400, json={"error": str(exc)})
        return httpx.Response(200, json=body)


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    tiny_cut(tmp_path / "curated")
    return tmp_path


def _executor(roots: Path, client: FakeActorClient, **kwargs: Any) -> SubstrateExecutor:
    templates = {p: _template(p) for p in SUBSTRATE_POLICIES}
    client.templates = {t.name: p for p, t in templates.items()}
    return SubstrateExecutor(
        client=client,
        registry=build_registry(AGENTS),
        settings=SubstrateSettings(),
        templates=templates,
        image_digest="sha256:" + "d" * 64,
        study_root=roots / "curated" / STUDY,
        tracker=SandboxTracker(roots / "actors.jsonl", FixedClock(NOW)),
        log=structlog.get_logger(),
        post=client.post,
        **kwargs,
    )


def test_a_run_returns_the_in_process_drafts_and_deletes_its_actor(roots: Path) -> None:
    client = FakeActorClient(templates={})
    executor = _executor(roots, client)
    plan = executor.plan("rbqm.kri_monitor", cut_trigger())
    assert plan.executor == "substrate"
    assert plan.substrate is not None
    assert plan.substrate.actor_uid is None  # not created yet
    result = executor.run("rbqm.kri_monitor", cut_trigger(), "DC-TINY", plan)
    registered = build_registry(AGENTS).get("rbqm.kri_monitor")
    reader = ParquetStudyReader(roots / "curated" / STUDY, "DC-TINY")
    expected = drafts_document(
        registered, run_agent(registered, cut_trigger(), reader, structlog.get_logger())
    )
    assert drafts_document(registered, result.drafts) == expected
    assert client.created == [plan.substrate.actor] == client.deleted
    assert result.executor == "substrate"
    assert result.policy_sha256 is None
    assert result.substrate is not None
    assert result.substrate.actor_uid == f"uid-{plan.substrate.actor}"
    assert result.substrate.golden_snapshot == GOLDEN
    assert result.substrate.template_sha256 == "7" * 64
    assert result.inputs_manifest_sha256 is not None
    assert [step for step, _ in result.phases_ms] == [
        "stage",
        "create",
        "resume",
        "proxy",
        "request",
        "delete",
    ]
    executor.drain()


def test_an_actor_with_any_snapshot_but_its_golden_one_fails_the_run(roots: Path) -> None:
    client = FakeActorClient(templates={})
    client.snapshots = [
        {"uuid": GOLDEN, "owner": "SNAPSHOT_OWNER_TAG"},
        {"uuid": "later", "owner": "SNAPSHOT_OWNER_ACTOR"},
    ]
    executor = _executor(roots, client)
    with pytest.raises(ExecutionError, match="snapshots other than its golden one"):
        executor.run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert client.live == {}  # deleted all the same


def test_a_failed_create_still_deletes_and_is_tracked(roots: Path) -> None:
    client = FakeActorClient(templates={}, create_error=NotFoundError("no such template"))
    executor = _executor(roots, client)
    with pytest.raises(ExecutionError, match="substrate failed"):
        executor.run("rbqm.csm", cut_trigger(), "DC-TINY")
    tracker = SandboxTracker(roots / "actors.jsonl", FixedClock(NOW))
    assert tracker.pending() == []


def test_a_policy_with_network_rules_has_no_template(roots: Path) -> None:
    client = FakeActorClient(templates={})
    with pytest.raises(ExecutionError, match="no substrate template may exist"):
        SubstrateExecutor(
            client=client,
            registry=build_registry(AGENTS),
            settings=SubstrateSettings(),
            templates={"rbqm.narrator": _template("rbqm.narrator")},
            image_digest="sha256:" + "d" * 64,
            study_root=roots,
            tracker=SandboxTracker(roots / "a.jsonl", FixedClock(NOW)),
            log=structlog.get_logger(),
        )


def test_foreign_snapshots_names_everything_but_the_golden_tag() -> None:
    actor = {"status": {"snapshots": [{"uuid": GOLDEN, "owner": "SNAPSHOT_OWNER_TAG"}]}}
    assert foreign_snapshots(actor, GOLDEN) == []
    actor["status"]["snapshots"].append({"uuid": GOLDEN, "owner": "SNAPSHOT_OWNER_ACTOR"})
    assert foreign_snapshots(actor, GOLDEN) == [GOLDEN]


def test_the_archive_is_the_same_bytes_for_the_same_files(roots: Path, tmp_path: Path) -> None:
    def build(dest: Path) -> bytes:
        staged = stage_run(
            agent_id="rbqm.csm",
            trigger=cut_trigger(),
            cut_id="DC-TINY",
            policy_name="rbqm",
            dest=dest,
            study_root=roots / "curated" / STUDY,
            safety_root=None,
            etmf_root=None,
            audit_root=None,
        )
        return archive(staged.stage)

    assert build(tmp_path / "a") == build(tmp_path / "b")


# ---- proof C4: an actor of each zone receives only that zone's folders


def _zone_roots(tmp_path: Path) -> tuple[Path, Event, Event]:
    """A cut with blinded/, ops/ and unblinded/, its safety database and eTMF snapshots,
    and one audit extract."""
    cut = tmf_base(tmp_path)
    ensure_clean_snapshot(
        etmf_root=tmp_path / "etmf",
        study_id=STUDY,
        cut=cut,
        ref=load_tmf_reference(),
        clock=FixedClock(NOW),
    )
    safety = tmp_path / "safety" / STUDY / "DC-BASE" / "blinded"
    safety.mkdir(parents=True)
    pl.DataFrame({"CASE_ID": ["C1"]}).write_parquet(safety / "safety_cases.parquet")
    (tmp_path / "safety" / STUDY / "DC-BASE" / "unblinded").mkdir()
    pl.DataFrame({"ARM": ["secret"]}).write_parquet(
        tmp_path / "safety" / STUDY / "DC-BASE" / "unblinded" / "truth.parquet"
    )
    review = cut_trigger("DC-BASE", seq=9).model_copy(
        update={"payload": AuditReviewRequested(study_id=STUDY, from_seq=1, through_seq=8)}
    )
    write_extract([], tmp_path / "audit" / "9" / "audit")
    return tmp_path, cut_trigger("DC-BASE", seq=2), review


EXPECTED_AREAS = {
    "rbqm": {"meta", "blinded", "ops"},
    "safety.sae_reconciler": {"meta", "blinded"},
    "quality": {"meta", "blinded", "tmf"},
    "quality.audit_trail": {"meta", "audit"},
}
AGENT_OF = {
    "rbqm": "rbqm.csm",
    "safety.sae_reconciler": "safety.sae_reconciler",
    "quality": "quality.tmf_completeness",
    "quality.audit_trail": "quality.audit_trail_reviewer",
}


@pytest.mark.parametrize("policy", SUBSTRATE_POLICIES)
def test_an_actor_receives_only_its_zones_folders(tmp_path: Path, policy: str) -> None:
    assert set(SUBSTRATE_POLICIES) == set(EXPECTED_AREAS)
    root, cut, review = _zone_roots(tmp_path)
    audit = policy == "quality.audit_trail"
    staged = stage_run(
        agent_id=AGENT_OF[policy],
        trigger=review if audit else cut,
        cut_id="LEDGER" if audit else "DC-BASE",
        policy_name=policy,
        dest=tmp_path / "stage",
        study_root=root / "curated" / STUDY,
        safety_root=root / "safety" / STUDY,
        etmf_root=root / "etmf" / STUDY,
        audit_root=root / "audit",
    )
    members = archive_members(archive(staged.stage))
    areas = {m.split("/", 1)[0] for m in members}
    assert areas == EXPECTED_AREAS[policy]
    assert areas <= set(ALL_POLICIES[policy].reads)
    assert not any("unblinded" in m or "truth" in m or m == "manifest.json" for m in members)
    if policy == "safety.sae_reconciler":
        assert "blinded/safety_cases.parquet" in members  # the safety database's blinded area
    # The actor's side: the template's --areas is the same set, and it refuses any other.
    command = zone_command(policy)
    assert set(command[command.index("--areas") + 1].split(",")) == set(ALL_POLICIES[policy].reads)
    for other in {"blinded", "ops", "unblinded", "tmf", "audit", "lm", "narr"} - areas:
        bad = io.BytesIO()
        with tarfile.open(fileobj=bad, mode="w") as tar:
            info = tarfile.TarInfo(f"{other}/x.parquet")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(ArchiveError):
            unpack(bad.getvalue(), tmp_path / f"unpack-{other}", areas_of(policy))


@pytest.mark.parametrize(
    "name",
    ["../x.parquet", "meta/../../x", "meta/a/b.parquet", "meta/.hidden", "/etc/passwd"],
)
def test_unpack_refuses_anything_but_area_slash_file(tmp_path: Path, name: str) -> None:
    bad = io.BytesIO()
    with tarfile.open(fileobj=bad, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (tmp_path / "u").mkdir()
    with pytest.raises(ArchiveError):
        unpack(bad.getvalue(), tmp_path / "u", ("meta", "blinded"))


def test_a_link_member_is_refused(tmp_path: Path) -> None:
    bad = io.BytesIO()
    with tarfile.open(fileobj=bad, mode="w") as tar:
        info = tarfile.TarInfo("meta/trigger.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    (tmp_path / "u").mkdir()
    with pytest.raises(ArchiveError):
        unpack(bad.getvalue(), tmp_path / "u", ("meta",))


def test_the_server_takes_one_run_and_checks_the_archive_sha(roots: Path, tmp_path: Path) -> None:
    staged = stage_run(
        agent_id="rbqm.csm",
        trigger=cut_trigger(),
        cut_id="DC-TINY",
        policy_name="rbqm",
        dest=tmp_path / "s",
        study_root=roots / "curated" / STUDY,
        safety_root=None,
        etmf_root=None,
        audit_root=None,
    )
    body = archive(staged.stage)
    state = _State(areas_of("rbqm"), CONFIG, 1 << 30)
    assert state.claim()
    with pytest.raises(ArchiveError, match="sha256"):
        run_once(state, "rbqm.csm", body, "0" * 64)
    out = run_once(state, "rbqm.csm", body, hashlib.sha256(body).hexdigest())
    assert out["exit_code"] == 0
    assert out["inputs_manifest_sha256"] == staged.inputs_manifest_sha256
    state.finish()
    assert not state.claim()  # one actor, one run

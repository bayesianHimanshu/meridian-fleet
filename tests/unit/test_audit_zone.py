"""The audit zone: policies/quality.audit_trail.yaml reads only meta and audit, no other
policy reads audit; a review stages only its trigger and the extract; the runner records
the reviewed seq range in ActorWoke and runs only in the operational executor."""

from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.quality.audit_trail_reviewer import AGENT_ID, AuditTrailReviewer
from meridian.agents.runner import AgentRunner, SandboxRequiredError
from meridian.audit.extract import extract_rows, write_extract
from meridian.domain import audit as a
from meridian.domain.enums import EventType, Role
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    AuditReviewRequested,
    Event,
    FindingRaised,
)
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.sandbox import policies
from meridian.sandbox.executor import InProcessExecutor
from meridian.sandbox.staging import StagingError, assert_clean_stage, stage_audit, uploads
from tests.api_support import T0, build

REPO = Path(__file__).resolve().parents[2]


def test_the_audit_policy_reads_only_meta_and_audit_and_has_no_network() -> None:
    policy = policies.ALL_POLICIES["quality.audit_trail"]
    assert policy.reads == ("meta", "audit")
    assert policy.network == ()
    assert policy.zone == "quality"
    assert policies.policy_for_agent(AGENT_ID) == "quality.audit_trail"
    assert policies.POLICY_BOUNDARY["quality.audit_trail"] == "audit"
    readers = sorted(n for n, p in policies.ALL_POLICIES.items() if "audit" in p.reads)
    assert readers == ["quality.audit_trail"]
    text = (REPO / "policies" / "quality.audit_trail.yaml").read_text()
    assert "network_policies: {}" in text
    assert "/stage/blinded" not in text
    assert "/stage/audit" in text


def _runner(tmp_path: Path, executor_name: str = "openshell") -> tuple[AgentRunner, SqliteLedger]:
    fx = build(tmp_path / "api", restricted=True)
    registry = build_registry(Settings().agents)
    executor = InProcessExecutor(
        registry=registry,
        reader_factory=lambda cut: (_ for _ in ()).throw(AssertionError("no cut is read")),
        log=structlog.get_logger(),
        audit_root=tmp_path / "audit",
    )
    executor.name = executor_name
    ledger = fx.ledger()
    runner = AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=executor,
        clock=FixedClock(T0),
        study_id="MRD-201",
        log=structlog.get_logger(),
        operational_cuts=["DC-BASE"],
    )
    return runner, ledger


def test_a_review_records_its_range_and_reads_only_the_extract(tmp_path: Path) -> None:
    runner, ledger = _runner(tmp_path)
    before = len(ledger.read())

    def prepare(trigger: Event, entries: list[Event]) -> None:
        p = trigger.payload
        assert isinstance(p, AuditReviewRequested)
        rows = extract_rows(
            entries, from_seq=p.from_seq, through_seq=p.through_seq, operational_cuts=["DC-BASE"]
        )
        write_extract(rows, tmp_path / "audit" / str(trigger.seq) / "audit")

    report = runner.run_audit_review(prepare)  # type: ignore[arg-type]
    assert report.failed == []
    events = [
        e for e in ledger.read(viewer_role=Role.UNBLINDED_STATISTICIAN) if isinstance(e, Event)
    ]
    trigger = events[before]
    assert isinstance(trigger.payload, AuditReviewRequested)
    assert trigger.payload.through_seq == before
    woke = next(
        e.payload
        for e in events
        if isinstance(e.payload, ActorWoke) and e.payload.agent_id == AGENT_ID
    )
    assert (woke.review_from_seq, woke.review_to_seq, woke.trigger_seq) == (1, before, trigger.seq)
    assert any(
        isinstance(e.payload, ActorSuspended) and e.payload.agent_id == AGENT_ID for e in events
    )
    # The fixture's kri_monitor run on DC-BASE has an image and a policy: no finding about it.
    raised = [
        e.payload
        for e in events
        if isinstance(e.payload, FindingRaised) and e.payload.finding.agent_id == AGENT_ID
    ]
    assert all(r.dedup_key.split("|")[4] == a.LEDGER_SCOPE for r in raised)
    # The data-cut pass never wakes the reviewer: it subscribes to audit reviews only.
    assert AuditTrailReviewer.subscribes_to == frozenset({EventType.AUDIT_REVIEW_REQUESTED})


def test_a_review_outside_the_sandbox_is_refused_before_any_event(tmp_path: Path) -> None:
    runner, ledger = _runner(tmp_path, executor_name="inprocess")
    before = len(ledger.read())
    with pytest.raises(SandboxRequiredError):
        runner.run_audit_review(lambda t, e: None)
    assert len(ledger.read()) == before


def test_an_audit_stage_holds_the_trigger_and_the_extract_only(tmp_path: Path) -> None:
    fx = build(tmp_path / "api", restricted=False)
    rows = extract_rows(
        fx.events, from_seq=1, through_seq=fx.events[-1].seq, operational_cuts=["DC-BASE"]
    )
    extract = write_extract(rows, tmp_path / "x" / "audit").parent
    stage = stage_audit(extract, fx.events[-1], tmp_path / "st")
    assert sorted(p.name for p in stage.iterdir()) == ["audit", "meta"]
    assert {dest for _, dest in uploads(stage)} == {"/stage/meta", "/stage/audit"}
    with pytest.raises(StagingError):
        assert_clean_stage(stage, ("blinded",))

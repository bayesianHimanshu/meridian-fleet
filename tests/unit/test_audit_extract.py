"""The audit extract: identifiers and hashes only. No evidence, narratives, titles, subject
ids or payloads of restricted events leave the host."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from meridian.audit.extract import SUBJECT_MASK, extract_rows, frame, write_extract
from meridian.domain import audit as a
from meridian.domain.enums import DecisionAction, FindingKind, Role, Severity, Visibility
from meridian.domain.events import (
    ActorWoke,
    DataCut,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
)
from meridian.domain.findings import Decision, Evidence, Finding
from tests.api_support import Fixture, build

SUBJECT = "201-1018-006"


def _ledger(tmp_path: Path) -> Fixture:
    """The standard API fixture (with its restricted finding and narrative, every restricted
    string containing SECRET), plus a subject-level safety finding, a narrative naming the
    subject, and decisions, one with the subject id typed into its reason."""
    fx = build(tmp_path, restricted=True)
    finding = Finding(
        kind=FindingKind.SAE_RECONCILIATION,
        agent_id="safety.sae_reconciler",
        agent_version="1.1.1",
        study_id="MRD-201",
        site_id="1018",
        severity=Severity.MEDIUM,
        title=f"Outcome of a serious AE of {SUBJECT} differs from its case",
        summary="Syncope: RECOVERED/RESOLVED in the clinical database, FATAL in the case.",
        evidence=(
            Evidence(
                source="ae",
                selector=f"USUBJID={SUBJECT}; AESEQ=4; AESER=Y",
                statistic="outcome:clinical",
                value=0.0,
                display="RECOVERED/RESOLVED",
            ),
        ),
        visibility=Visibility.BLINDED,
        visible_to=frozenset({Role.MEDICAL_MONITOR}),
        score=0.5,
    )
    raised = fx.raise_finding(finding, check=f"outcome_mismatch:{SUBJECT}/4")
    assert isinstance(raised.payload, FindingRaised)
    fid = raised.payload.finding.finding_id
    assert fid is not None
    fx.append(
        FindingNarrative(
            finding_id=fid,
            finding_seq=raised.seq,
            agent_id="safety.local_narrator",
            agent_version="1.0.2",
            status="accepted",
            headline=f"Site 1018, subject {SUBJECT}: Outcome differs",
            explanation="The outcome is RECOVERED/RESOLVED in the clinical database.",
            model="phi4-mini:3.8b-q4_K_M",
            model_digest="1" * 64,
            request_sha256="2" * 64,
            response_sha256="3" * 64,
        )
    )
    fx.append(
        HumanDecision(
            decision=Decision(
                finding_id=fid,
                action=DecisionAction.DISMISSED,
                actor="karan.mehta",
                role=Role.MEDICAL_MONITOR,
                reason=f"Case of {SUBJECT} corrected at the site; outcomes now agree.",
            )
        )
    )
    return fx


def test_the_extract_holds_no_evidence_narrative_title_or_subject_id(tmp_path: Path) -> None:
    fx = _ledger(tmp_path)
    rows = extract_rows(
        fx.events, from_seq=1, through_seq=fx.events[-1].seq, operational_cuts=["DC-BASE"]
    )
    assert [r[a.SEQ] for r in rows] == [e.seq for e in fx.events]
    assert all(set(r) == set(a.COLUMNS) for r in rows)
    text = json.dumps(rows)
    assert SUBJECT not in text
    assert not re.search(r"\b\d{3}-\d{4}-\d{3}\b", text)
    assert "SECRET" not in text  # nothing of the restricted finding, its narrative or decision
    for leaked in ("RECOVERED/RESOLVED", "Outcome of a serious AE", "Syncope", "18 observed"):
        assert leaked not in text
    decision = next(r for r in rows if r[a.TYPE] == "human_decision")
    assert (
        decision[a.DECISION_REASON]
        == f"Case of {SUBJECT_MASK} corrected at the site; outcomes now agree."
    )
    assert decision[a.ACTOR] == "karan.mehta"
    assert decision[a.ROLE] == "medical_monitor"


def test_restricted_events_keep_only_seq_hashes_type_and_time(tmp_path: Path) -> None:
    fx = _ledger(tmp_path)
    rows = extract_rows(
        fx.events, from_seq=1, through_seq=fx.events[-1].seq, operational_cuts=["DC-BASE"]
    )
    restricted = [e.seq for e in fx.events if e.visibility is Visibility.RESTRICTED]
    assert restricted
    for r in rows:
        if r[a.SEQ] in restricted:
            assert r[a.REDACTED] == "Y"
            filled = {k for k, v in r.items() if v is not None}
            assert filled == set(a.REDACTED_COLUMNS)


def test_runs_carry_their_cut_scope_and_findings_a_group_not_a_check(tmp_path: Path) -> None:
    fx = _ledger(tmp_path)
    rows = extract_rows(
        fx.events, from_seq=1, through_seq=fx.events[-1].seq, operational_cuts=["DC-BASE"]
    )
    woke = next(r for r in rows if r[a.TYPE] == "actor_woke")
    assert (woke[a.CUT_ID], woke[a.CUT_SCOPE], woke[a.RUN_STATUS]) == (
        "DC-BASE",
        "operational",
        "started",
    )
    raised = [r for r in rows if r[a.TYPE] == "finding_raised" and r[a.REDACTED] == "N"]
    assert {r[a.FINDING_GROUP] for r in raised} == set(range(1, len(raised) + 1))
    opaque = next(r for r in rows if r[a.TYPE] == "data_cut" and r[a.CUT_ID] != "DC-BASE")
    assert opaque[a.CUT_SCOPE] == "evaluation"


def test_a_range_is_exported_and_written_once(tmp_path: Path) -> None:
    fx = _ledger(tmp_path)
    rows = extract_rows(fx.events, from_seq=2, through_seq=4, operational_cuts=["DC-BASE"])
    assert [r[a.SEQ] for r in rows] == [2, 3, 4]
    path = write_extract(rows, tmp_path / "audit")
    back = pl.read_parquet(path)
    assert back.columns == list(a.COLUMNS)
    assert back.equals(frame(rows))
    with pytest.raises(FileExistsError):
        write_extract(rows, tmp_path / "audit")


def test_a_runs_start_goes_to_started_at_and_older_runs_leave_it_empty(tmp_path: Path) -> None:
    """Phase 4a step 3: ActorWoke is appended after the run and carries its start."""
    fx = build(tmp_path)
    cut = next(e for e in fx.events if isinstance(e.payload, DataCut))
    started = datetime(2026, 11, 7, 9, 59, 58, tzinfo=UTC)
    new = fx.append(ActorWoke(agent_id="rbqm.csm", trigger_seq=cut.seq, started_at=started))
    old = fx.append(ActorWoke(agent_id="rbqm.csm", trigger_seq=cut.seq))
    rows = {
        r[a.SEQ]: r
        for r in extract_rows(
            fx.events, from_seq=1, through_seq=fx.events[-1].seq, operational_cuts=["DC-BASE"]
        )
    }
    assert rows[new.seq][a.STARTED_AT] == "2026-11-07T09:59:58.000000Z"
    assert rows[new.seq][a.RECORDED_AT] != rows[new.seq][a.STARTED_AT]
    assert rows[old.seq][a.STARTED_AT] is None

"""quality.audit_trail_reviewer, check by check: each accepts its clean example and flags its
fault; the thresholds' boundaries; malformed rows become audit_data_quality findings."""

from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest
import structlog

from meridian.agents.base import AgentContext
from meridian.agents.quality.audit_trail_reviewer import AuditTrailReviewer, review
from meridian.audit.extract import frame
from meridian.data.study_store import MissingTableError
from meridian.domain import audit as a
from meridian.domain.enums import Role, Severity
from meridian.domain.events import AuditReviewRequested, DataCut, Event
from meridian.ledger.hashing import GENESIS_HASH
from meridian.platform.agent_settings import AuditTrailReviewerConfig
from meridian.platform.clock import FixedClock

CFG = AuditTrailReviewerConfig()
T0 = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)


def h(n: int) -> str:
    return f"{n:064x}"


class Ledger:
    """Rows typed in seq order, hashed h(1), h(2), ..., each linked to the one before."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, typ: str, at: datetime = T0, **values: Any) -> int:
        seq = len(self.rows) + 1
        prev = self.rows[-1][a.HASH] if self.rows else GENESIS_HASH
        row: dict[str, Any] = dict.fromkeys(a.COLUMNS)
        row |= {
            a.SEQ: seq,
            a.HASH: h(seq),
            a.PREV_HASH: prev,
            a.TYPE: typ,
            a.RECORDED_AT: at.isoformat(),
            a.REDACTED: "N",
            **values,
        }
        self.rows.append(row)
        return seq

    def cut(self, cut: str = "DC-BASE", scope: str = a.OPERATIONAL) -> int:
        return self.add("data_cut", CUT_ID=cut, CUT_SCOPE=scope)

    def run(
        self,
        trig: int,
        agent: str = "rbqm.csm",
        version: str = "2.1.0",
        *,
        done: bool = True,
        executor: str = "openshell",
        image: bool = True,
        cut: str = "DC-BASE",
        scope: str = a.OPERATIONAL,
        at: datetime = T0,
    ) -> None:
        common = {
            a.AGENT: agent,
            a.VERSION: version,
            a.TRIGGER_SEQ: trig,
            a.CUT_ID: cut,
            a.CUT_SCOPE: scope,
            a.EXECUTOR: executor,
            a.ACTOR: agent,
        }
        self.add("actor_woke", at, RUN_STATUS=a.STARTED, **common)
        if done:
            extra = (
                {a.IMAGE_DIGEST: "sha256:" + "a" * 64, a.POLICY_SHA256: "b" * 64} if image else {}
            )
            self.add("actor_suspended", at, RUN_STATUS=a.COMPLETED, **common, **extra)

    def raised(
        self, fid: str, group: int, version: str = "2.1.0", agent: str = "rbqm.kri_monitor"
    ) -> int:
        return self.add(
            "finding_raised",
            FINDING_ID=fid,
            FINDING_GROUP=group,
            AGENT=agent,
            VERSION=version,
            CUT_ID="DC-BASE",
            CUT_SCOPE=a.OPERATIONAL,
        )

    def decide(
        self,
        fid: str,
        *,
        actor: str = "asha.rao",
        action: str = "accepted",
        reason: str = "Entry lag at the site confirmed; CRA visit booked.",
        at: datetime = T0,
    ) -> int:
        return self.add(
            "human_decision",
            at,
            ACTOR=actor,
            ROLE="clinical_ops_lead",
            FINDING_ID=fid,
            DECISION_ACTION=action,
            DECISION_REASON=reason,
        )

    def table(self) -> pl.DataFrame:
        return frame(self.rows)

    def found(self, cfg: AuditTrailReviewerConfig = CFG, through: int | None = None) -> set[str]:
        last = through or len(self.rows)
        return {f"{x.check}:{x.record}" for x in review(self.table(), 1, last, cfg, NOW)}


def test_a_clean_ledger_gives_nothing() -> None:
    lg = Ledger()
    c = lg.cut()
    lg.run(c)
    lg.raised("RBQM-0001", 1)
    lg.decide("RBQM-0001")
    assert lg.found() == set()


def test_chain_linkage_flags_a_gap_a_broken_link_a_short_extract_and_a_bad_genesis() -> None:
    lg = Ledger()
    for _ in range(5):
        lg.cut()
    gap = Ledger()
    gap.rows = [dict(r) for r in lg.rows if r[a.SEQ] != 3]
    gap.rows[2][a.PREV_HASH] = h(2)  # seq 4 links to seq 2: only the gap is wrong
    assert gap.found(through=5) == {"audit_chain_linkage:gap-3-4"}
    broken = Ledger()
    broken.rows = [dict(r) for r in lg.rows]
    broken.rows[3][a.PREV_HASH] = h(99)
    assert broken.found() == {"audit_chain_linkage:link-4"}
    assert lg.found(through=7) == {"audit_chain_linkage:end-5"}
    genesis = Ledger()
    genesis.rows = [dict(r) for r in lg.rows]
    genesis.rows[0][a.PREV_HASH] = h(42)
    assert genesis.found() == {"audit_chain_linkage:link-1"}
    hit = next(x for x in review(gap.table(), 1, 5, CFG, NOW))
    assert any(
        "full hash verification stays on the host" in (e.display or "") for e in hit.evidence
    )


def test_unsandboxed_operational_runs_are_flagged_and_evaluation_ones_are_not() -> None:
    lg = Ledger()
    op, ev = lg.cut(), lg.cut("DC-1a2b3c4d", a.EVALUATION)
    lg.run(ev, executor="inprocess", image=False, cut="DC-1a2b3c4d", scope=a.EVALUATION)
    assert lg.found() == set()
    lg.run(op, "rbqm.qtl_detector", image=False)
    lg.run(op, "rbqm.kri_monitor", "2.2.0", executor="inprocess", image=False)
    lg.run(op, "rbqm.kri_monitor", "", executor="", image=False)  # a phase 1 run, no version
    assert lg.found() == {
        "audit_unsandboxed_operational_run:rbqm.qtl_detector/2.1.0/1",
        "audit_unsandboxed_operational_run:rbqm.kri_monitor/2.2.0/1",
        "audit_unsandboxed_operational_run:rbqm.kri_monitor//1",
    }


def test_a_failed_run_is_unresolved_until_the_same_agent_completes_on_the_cut() -> None:
    lg = Ledger()
    op = lg.cut()
    lg.run(op, "safety.sae_reconciler", "1.0.0", done=False)
    lg.run(op, "rbqm.csm")  # another agent does not resolve it
    assert lg.found() == {"audit_failed_run_unresolved:safety.sae_reconciler/1.0.0/1/2"}
    lg.run(op, "safety.sae_reconciler", "1.1.1")  # a later version on the same cut does
    assert lg.found() == set()
    # A run that started just before the review may still be running.
    recent = Ledger()
    c = recent.cut()
    recent.run(c, "rbqm.csm", done=False, at=NOW - timedelta(seconds=30))
    assert recent.found() == set()


def _decisions(n: int, spacing: timedelta, **kw: Any) -> Ledger:
    lg = Ledger()
    lg.cut()
    for i in range(n):
        lg.decide(
            f"RBQM-{i + 1:04d}",
            at=T0 + i * spacing,
            reason=f"Finding {i + 1} checked with the site; CRA follow-up booked.",
            **kw,
        )
    return lg


def test_rapid_decisions_flag_at_the_threshold_and_not_below() -> None:
    assert _decisions(10, timedelta(minutes=5) / 9).found() == {"audit_rapid_decisions:asha.rao/2"}
    assert _decisions(9, timedelta(minutes=5) / 8).found() == set()
    assert _decisions(10, timedelta(minutes=5) / 9 + timedelta(seconds=1)).found() == set()
    assert _decisions(10, timedelta(minutes=60) / 9).found() == set()
    hit = next(x for x in review(_decisions(12, timedelta(seconds=10)).table(), 1, 13, CFG, NOW))
    assert hit.actor == "asha.rao"
    assert "not proof" in hit.summary
    strict = AuditTrailReviewerConfig(rapid_decisions=5, rapid_window_minutes=1.0)
    assert _decisions(5, timedelta(seconds=15)).found(strict) == {
        "audit_rapid_decisions:asha.rao/2"
    }


def test_reason_quality_flags_thin_placeholder_and_reused_reasons() -> None:
    lg = Ledger()
    lg.cut()
    hour = timedelta(hours=1)
    ok = lg.decide("RBQM-0001", action="dismissed", reason="ok", at=T0)
    na = lg.decide("RBQM-0002", action="dismissed", reason="N/A.", at=T0 + hour)
    short = lg.decide("RBQM-0003", action="dismissed", reason="dup of 0002", at=T0 + 2 * hour)
    lg.decide("RBQM-0004", action="accepted", reason="ok", at=T0 + 3 * hour)  # not a dismissal
    reused = "Known natural breach at this site in the clean base cut (phase 1)."
    first = lg.decide("RBQM-0005", action="dismissed", reason=reused, at=T0 + 4 * hour)
    for i in range(3):
        lg.decide(f"RBQM-{i + 6:04d}", action="dismissed", reason=reused, at=T0 + (5 + i) * hour)
    lg.decide("RBQM-0009", action="dismissed", reason=reused, actor="karan.mehta", at=T0 + 9 * hour)
    assert lg.found() == {
        f"audit_reason_quality:{ok}",
        f"audit_reason_quality:{na}",
        f"audit_reason_quality:{short}",
        f"audit_reason_quality:asha.rao/reused/{first}",
    }
    three = Ledger()
    three.cut()
    for i in range(3):
        three.decide(f"RBQM-{i + 1:04d}", action="dismissed", reason=reused, at=T0 + i * hour)
    assert three.found() == set()  # identical to only 2 others


def test_decisions_after_retirement_or_supersession_are_flagged() -> None:
    lg = Ledger()
    c = lg.cut()
    lg.run(c, "rbqm.kri_monitor", "2.1.0")
    lg.raised("RBQM-0001", 1)
    before = lg.decide("RBQM-0001", action="escalated", reason="Needs the medical monitor's view.")
    lg.run(c, "rbqm.kri_monitor", "2.2.0")  # completes without raising group 1: retired
    after = lg.decide("RBQM-0001")
    assert lg.found() == {f"audit_decision_on_retired:{after}"}
    assert before < after
    sup = Ledger()
    c = sup.cut()
    sup.raised("RBQM-0001", 1)
    sup.raised("RBQM-0002", 1, version="2.2.0")  # the same thing, newer version
    late = sup.decide("RBQM-0001")
    sup.decide("RBQM-0002")
    assert sup.found() == {f"audit_decision_on_retired:{late}"}


def test_malformed_rows_are_data_quality_findings_and_never_fail() -> None:
    lg = Ledger()
    lg.cut()
    lg.decide("RBQM-0001")
    lg.cut()
    lg.rows[1][a.DECISION_ACTION] = "approved"
    lg.rows[2][a.HASH] = "not a hash"
    lg.rows[2][a.RECORDED_AT] = "yesterday"
    bad = dict(lg.rows[2])
    bad[a.SEQ] = None
    lg.rows.append(bad)
    found = lg.found(through=3)
    assert found >= {
        "audit_data_quality:2#DECISION_ACTION",
        "audit_data_quality:3#HASH",
        "audit_data_quality:3#RECORDED_AT",
        "audit_data_quality:row-4#SEQ",
    }
    assert review(pl.DataFrame({"X": [1]}), 1, 1, CFG, NOW)[0].check == a.DATA_QUALITY


class _Reader:
    study_id = "MRD-201"
    cut_id = a.LEDGER_SCOPE

    def __init__(self, t: pl.DataFrame | None) -> None:
        self._t = t

    def table(self, name: str) -> pl.DataFrame:
        if self._t is None:
            raise MissingTableError(name)
        return self._t

    def tables(self) -> list[str]:
        return [] if self._t is None else [a.AUDIT_EXTRACT]


def _trigger(through: int) -> Event:
    return Event(
        seq=through + 1,
        event_id="00000000-0000-0000-0000-000000000001",  # type: ignore[arg-type]
        occurred_at=NOW,
        recorded_at=NOW,
        actor="meridian.audit",
        study_id="MRD-201",
        visibility="blinded",  # type: ignore[arg-type]
        payload=AuditReviewRequested(study_id="MRD-201", through_seq=through),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def test_drafts_are_kind_audit_on_the_ledger_scope_naming_the_person_under_review() -> None:
    lg = _decisions(10, timedelta(seconds=20))
    ctx = AgentContext(
        study=_Reader(lg.table()), clock=FixedClock(NOW), config=CFG, log=structlog.get_logger()
    )
    (draft,) = AuditTrailReviewer().run(_trigger(len(lg.rows)), ctx)
    assert draft.kind.value == "audit"
    assert draft.dedup_key.split("|")[4] == a.LEDGER_SCOPE
    assert draft.actor_under_review == "asha.rao"
    assert set(draft.visible_to or ()) == {Role.QA_AUDITOR, Role.CLINICAL_OPS_LEAD}
    assert draft.severity is Severity.MEDIUM
    empty = AgentContext(
        study=_Reader(None), clock=FixedClock(NOW), config=CFG, log=structlog.get_logger()
    )
    assert AuditTrailReviewer().run(_trigger(3), empty) == []
    not_a_review = _trigger(3).model_copy(
        update={"payload": DataCut(cut_id="DC-BASE", study_id="MRD-201", path="x")}
    )
    with pytest.raises(ValueError, match="not an audit review"):
        AuditTrailReviewer().run(not_a_review, ctx)


def test_chain_and_unsandboxed_findings_are_high() -> None:
    lg = Ledger()
    c = lg.cut()
    lg.run(c, executor="inprocess", image=False)
    lg.rows[1][a.PREV_HASH] = h(77)
    sev = {x.check: x for x in review(lg.table(), 1, len(lg.rows), CFG, NOW)}
    from meridian.agents.quality.audit_trail_reviewer import SEVERITY

    assert SEVERITY[a.CHAIN_LINKAGE] is Severity.HIGH
    assert SEVERITY[a.UNSANDBOXED_OPERATIONAL_RUN] is Severity.HIGH
    assert set(sev) == {a.CHAIN_LINKAGE, a.UNSANDBOXED_OPERATIONAL_RUN}

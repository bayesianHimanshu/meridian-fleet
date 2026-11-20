"""Ledger events from a translational bridge stress case (tests/fixtures/bridge_stress,
docs/translational-notes.md), so a case runs through the host's own extract writer and
the agent, as a real run does. Nothing here is read by the agent itself."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from meridian.agents.base import FindingDraft
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import DecisionAction, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, EventPayload, FindingRaised, HumanDecision, NoaelDecision
from meridian.domain.findings import Decision, Evidence, Finding

T0 = datetime(2026, 11, 19, tzinfo=UTC)
RATIONALE = "Stress case rationale. " * 10


def _event(seq: int, payload: EventPayload, actor: str, study: str) -> Event:
    return Event(
        seq=seq,
        event_id=uuid.UUID(int=seq),
        occurred_at=T0,
        recorded_at=T0,
        actor=actor,
        study_id=study,
        visibility=Visibility.BLINDED,
        payload=payload,
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def _finding(
    fid: str, title: str, kind: FindingKind, agent: str, study: str, ev: tuple[Evidence, ...]
) -> Finding:
    return Finding(
        finding_id=fid,
        kind=kind,
        agent_id=agent,
        agent_version="1.0.0",
        study_id=study,
        severity=Severity.MEDIUM,
        title=title,
        summary=title,
        evidence=ev,
        visibility=Visibility.BLINDED,
        visible_to=frozenset({Role.TOXICOLOGIST, Role.MEDICAL_MONITOR}),
        score=0.5,
    )


def case_events(
    case: Mapping[str, Any], cut: str, study: str | None = None
) -> tuple[list[Event], Event]:
    """(the events, the trigger): the cited findings first (seq 1, 2, ...), then the
    decisions at their own seqs, then the proposal and its acceptance if any. The trigger is
    the latest decision, or the acceptance when there is one."""
    study = study or case["study_id"]
    events: list[Event] = []
    for i, f in enumerate(case["findings"], start=1):
        ev = Evidence(
            source="send:x", selector="SEX=M", statistic="x", value=1.0, display=f["title"]
        )
        finding = _finding(f["finding_id"], f["title"], FindingKind.TOX_SIGNAL,
                           "preclinical.tox_study_monitor", study, (ev,))  # fmt: skip
        key = make_dedup_key(agent_id="preclinical.tox_study_monitor", agent_version="1.1.0",
                             kind=FindingKind.TOX_SIGNAL, study_id=study, site_id=None,
                             cut_id=cut, check=f["check"])  # fmt: skip
        events.append(_event(i, FindingRaised(finding=finding, dedup_key=key), "agent", study))
    for d in case["decisions"]:
        payload = NoaelDecision(
            decision_id=d["decision_id"], study_id=study, sex=d["sex"],
            established=d["established"], noael_dose=d["noael_dose"], loael_dose=d["loael_dose"],
            dose_unit=d["dose_unit"], finding_ids=tuple(d["finding_ids"]), rationale=RATIONALE,
            decided_by=d["decided_by"], supersedes=d["supersedes"],
        )  # fmt: skip
        events.append(_event(d["seq"], payload, d["decided_by"], study))
    trigger = max(events, key=lambda e: e.seq)
    p = case.get("proposal")
    if p is not None:
        basis = f"DECISION={p['basis_decision_id']}; SEQ=1"
        prop_ev = (
            Evidence(
                source="ledger:noael_decision",
                selector=basis,
                statistic="mrsd:noael_mg_per_kg",
                value=1.0,
                role="primary",
                display="NOAEL",
            ),
            Evidence(
                source="config",
                selector="safety_factor",
                statistic="mrsd:mrsd_mg_per_kg",
                value=p["mrsd_mg_per_kg"],
                role="primary",
                display="MRSD",
            ),
            Evidence(
                source="config",
                selector="human_weight_kg",
                statistic="mrsd:starting_dose_mg",
                value=p["starting_dose_mg"],
                threshold=p["human_weight_kg"],
                role="primary",
                display="start",
            ),
        )
        proposal = _finding(
            p["finding_id"],
            "Proposed starting dose",
            FindingKind.DOSE_PROPOSAL,
            "preclinical.dose_translation",
            study,
            prop_ev,
        )
        key = make_dedup_key(
            agent_id="preclinical.dose_translation",
            agent_version="1.0.0",
            kind=FindingKind.DOSE_PROPOSAL,
            study_id=study,
            site_id=None,
            cut_id=cut,
            check=f"dose_proposal:{p['basis_decision_id']}",
        )
        events.append(
            _event(p["seq"], FindingRaised(finding=proposal, dedup_key=key), "agent", study)
        )
        if p["status"] != "open":
            decision = Decision(
                finding_id=p["finding_id"],
                action=DecisionAction(p["status"]),
                actor=p["decided_by"],
                role=Role.MEDICAL_MONITOR,
                reason="Stress case.",
            )
            trigger = _event(p["seq"] + 1, HumanDecision(decision=decision), p["decided_by"], study)
            events.append(trigger)
    return events, trigger


def outcome(drafts: list[FindingDraft]) -> dict[str, Any]:
    """The comparable result of a run: raised, organs with their finding ids, unmapped ids,
    and the two ratios."""
    if not drafts:
        return {
            "raised": False,
            "organs": {},
            "unmapped": [],
            "noael_ratio": None,
            "loael_ratio": None,
        }
    (d,) = drafts
    organs: dict[str, list[str]] = {}
    unmapped: list[str] = []
    ratios: dict[str, float | None] = {"noael_ratio": None, "loael_ratio": None}
    for e in d.evidence:
        fields = dict(part.split("=", 1) for part in e.selector.split("; ") if "=" in part)
        if e.statistic == "monitoring:finding":
            organs.setdefault(fields["ORGAN"], []).append(fields["FINDING"])
        elif e.statistic == "monitoring:unmapped":
            unmapped.append(fields["FINDING"])
        elif e.statistic in ("monitoring:noael_margin", "monitoring:loael_margin"):
            ratios[e.statistic.split(":")[1].replace("_margin", "_ratio")] = round(e.value, 6)
    return {"raised": True, "organs": organs, "unmapped": unmapped, **ratios}

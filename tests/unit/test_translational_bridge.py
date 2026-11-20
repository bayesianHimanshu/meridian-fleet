"""preclinical.translational_bridge (phase 5 step 3a): organ mapping on real checks, the
margins against the starting dose with the dose proposal's own factor, unmapped findings
reported, nothing without a decision, and the run through the host's extract writer."""

import json
from pathlib import Path
from typing import Any

import pytest
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.preclinical.translational_bridge import TranslationalBridge
from meridian.data.study_store import LayeredStudyReader, NoaelStageReader
from meridian.domain.enums import FindingKind
from meridian.domain.events import Event
from meridian.platform.clock import FixedClock
from meridian.platform.config import AgentsSettings, Settings
from meridian.preclinical.bridge import organ_of
from meridian.preclinical.bridge_cases import case_events, outcome
from meridian.preclinical.inputs import write_noael_extract
from meridian.preclinical.synth import STUDY, StudySpec
from tests.support import T0
from tests.tox_support import CUT, write_study


def cfg() -> Any:
    return Settings().agents.translational_bridge


@pytest.mark.parametrize(
    ("check", "organ"),
    [
        ("dose_related_incidence:mi:HEART:INFLAMMATION:M:main", "heart"),
        ("dose_related_change:lb:AST:F:main", "liver"),
        ("dose_related_incidence:mi:LIVER:NECROSIS:F:main", "liver"),
        ("recovery_only_incidence:mi:LIVER:VACUOLIZATION:M:recovery", "liver"),
        ("neoplasm:mi:LIVER:HEPATOCELLULAR CARCINOMA:M", "liver"),
        ("dose_related_change:lb:CREAT:M:main", "kidney"),
        ("dose_related_change:lb:HGB:F:main", "haematology"),
        ("dose_related_change:bw:BW:M:main", "body_weight"),
        ("organ_weight_secondary_to_bw:M:main:r3", "body_weight"),
        ("dose_related_incidence:mi:ADRENAL GLAND:VACUOLIZATION:M:main", None),
        ("unscheduled_death:MRD-101-TOX-01-4003", None),
    ],
)
def test_organ_of(check: str, organ: str | None) -> None:
    found = organ_of(check, cfg().mapping)
    assert (found.id if found else None) == organ


def test_the_margins_use_the_dose_proposals_own_factor() -> None:
    agents = Settings().agents
    assert agents.translational_bridge.dose == agents.dose_translation
    with pytest.raises(ValueError, match="dose_translation"):
        AgentsSettings.model_validate(
            {
                "qtl_detector": {"discontinuation_limit": 0.5},
                "dose_translation": agents.dose_translation.model_dump(),
                "translational_bridge": {"dose": agents.dose_translation.model_dump()},
            }
        )


# MRD-101's current decisions, the findings they cite and PRE-0232, as recorded (seqs
# renumbered): the pre-registered expectation (docs/translational-notes.md).
MRD101: dict[str, Any] = {
    "study_id": STUDY,
    **json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "bridge_mrd101.json").read_text()
    ),
}


def _run(tmp_path: Path, case: dict[str, Any]) -> tuple[list[FindingDraft], Event]:
    send = write_study(tmp_path / "send", StudySpec(seed=3))
    events, trigger = case_events(case, CUT, STUDY)
    root = write_noael_extract(events, trigger, CUT, tmp_path / "noael")
    reader = LayeredStudyReader(
        send, NoaelStageReader.at_cut_dir(root, cut_id=CUT, study_id=send.study_id)
    )
    ctx = AgentContext(study=reader, clock=FixedClock(T0), config=cfg(), log=structlog.get_logger())
    return list(TranslationalBridge().run(trigger, ctx)), trigger


def test_the_preregistered_result_for_mrd101(tmp_path: Path) -> None:
    drafts, trigger = _run(tmp_path, MRD101)
    assert trigger.seq == 59  # the acceptance, after the proposal
    assert outcome(drafts) == {
        "raised": True,
        "organs": {
            "liver": ["PRE-0165", "PRE-0198", "PRE-0210", "PRE-0214"],
            "heart": ["PRE-0155", "PRE-0196"],
        },
        "unmapped": [],
        "noael_ratio": 10.0,
        "loael_ratio": 100.0,
    }
    (d,) = drafts
    assert d.kind is FindingKind.MONITORING_RECOMMENDATION
    assert d.dedup_key.endswith("|monitoring:NOAEL-cd4154d72a20:PRE-0232")
    assert d.title == "Monitoring recommendation for MRD-101's first-in-human study: liver, heart"
    displays = {e.statistic: e.display or "" for e in d.evidence}
    assert "ALT or AST >8xULN" in displays["monitoring:source:liver"] or any(
        "ALT or AST >8xULN" in (e.display or "") for e in d.evidence
    )
    assert (
        "LOAEL 20 mg/kg as a human equivalent dose: 20 / 6.2 = 3.23 mg/kg, 194 mg"
        in (displays["monitoring:loael_hed_mg"])
    )
    assert "accepted by karan.mehta" in displays["monitoring:starting_dose_mg"]
    assert "proposal, not a decision" in d.summary


def test_no_decision_raises_nothing(tmp_path: Path) -> None:
    case = {**MRD101, "decisions": [], "proposal": None}
    events, _ = case_events({**MRD101, "proposal": None}, CUT, STUDY)
    send = write_study(tmp_path / "send", StudySpec(seed=3))
    root = write_noael_extract(events, events[-1], CUT, tmp_path / "noael")
    reader = LayeredStudyReader(
        send, NoaelStageReader.at_cut_dir(root, cut_id=CUT, study_id=send.study_id)
    )
    first = events[0]  # a finding's seq is before every decision: none is current
    ctx = AgentContext(study=reader, clock=FixedClock(T0), config=cfg(), log=structlog.get_logger())
    del case
    assert (
        list(TranslationalBridge().run(events[-1].model_copy(update={"seq": first.seq}), ctx)) == []
    )


def test_an_open_proposal_is_said_to_be_not_accepted(tmp_path: Path) -> None:
    case = {**MRD101, "proposal": {**MRD101["proposal"], "status": "open", "decided_by": ""}}
    drafts, trigger = _run(tmp_path, case)
    assert trigger.seq == 43  # no acceptance: the latest decision
    (d,) = drafts
    start = next(e for e in d.evidence if e.statistic == "monitoring:starting_dose_mg")
    assert "open, not accepted" in (start.display or "")
    assert outcome(drafts)["noael_ratio"] == 10.0


def test_a_proposal_on_a_revised_noael_is_not_used(tmp_path: Path) -> None:
    """The NOAEL is revised after the proposal: the old proposal rests on a decision that
    is no longer current, so the margins have no starting dose until a new one exists."""
    revised = {
        "seq": 70, "decision_id": "NOAEL-0000000000aa", "sex": "combined", "established": True,
        "noael_dose": 20.0, "loael_dose": 200.0, "dose_unit": "mg/kg", "decided_by": "leela.nair",
        "supersedes": "NOAEL-cd4154d72a20", "finding_ids": ["PRE-0155"],
    }  # fmt: skip
    case = {
        **MRD101,
        "decisions": [*MRD101["decisions"], revised],
        "proposal": {**MRD101["proposal"], "status": "open", "decided_by": ""},
    }
    drafts, trigger = _run(tmp_path, case)
    assert trigger.seq == 70
    result = outcome(drafts)
    assert result["organs"] == {"liver": ["PRE-0165", "PRE-0198", "PRE-0210", "PRE-0214"],
                                "heart": ["PRE-0155", "PRE-0196"]}  # fmt: skip
    assert (result["noael_ratio"], result["loael_ratio"]) == (None, None)
    (d,) = drafts
    assert d.dedup_key.endswith("monitoring:NOAEL-0000000000aa:none")

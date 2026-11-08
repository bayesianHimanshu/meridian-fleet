"""The SAE stress set (tests/fixtures/sae_stress/): hand-written pairs, run through
safety.sae_reconciler as they are.

The tables are built straight from the JSON (no generator, no normalisation of ours). The
test pins each case's `actual` outcome for the current agent version, recorded after its
run, so a change in behaviour shows up here; it does not test against the ideal. Where ideal and actual differ,
that is the backlog for the next agent version (docs/safety-eval.md, "Stress set").
"""

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import structlog

from meridian.agents.base import AgentContext
from meridian.agents.safety.sae_reconciler import VERSION, SaeReconciler
from meridian.domain.events import DataCut, Event
from meridian.platform.agent_settings import SaeReconcilerConfig
from meridian.platform.clock import FixedClock
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sae_stress"
CASES = sorted(FIXTURES.glob("*.json"))
AE_CRITERIA = ("AESDTH", "AESLIFE", "AESHOSP", "AESDISAB", "AESCONG")
CASE_CRITERIA = ("SDTH", "SLIFE", "SHOSP", "SDISAB", "SCONG")
CUT = "DC-STRESS"


def load(path: Path) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(path.read_text())
    return spec


def _frame(rows: list[dict[str, Any]], defaults: dict[str, Any]) -> pl.DataFrame:
    filled = [{**defaults, **r} for r in rows]
    columns = list(dict.fromkeys(k for r in filled for k in r))
    return pl.DataFrame([{c: r.get(c) for c in columns} for r in filled])


def tables(spec: dict[str, Any]) -> dict[str, pl.DataFrame]:
    ae = _frame(spec["ae"], {"STUDYID": STUDY, "DOMAIN": "AE", **dict.fromkeys(AE_CRITERIA, "N")})
    cases = _frame(spec["safety_cases"], dict.fromkeys(CASE_CRITERIA, "N"))
    return {"dm": pl.DataFrame(spec["dm"]), "ae": ae, "safety_cases": cases}


class _Reader:
    study_id = STUDY
    cut_id = CUT

    def __init__(self, t: dict[str, pl.DataFrame]) -> None:
        self._t = t

    def table(self, name: str) -> pl.DataFrame:
        return self._t[name]

    def tables(self) -> list[str]:
        return sorted(self._t)


def outcome(spec: dict[str, Any]) -> list[str]:
    """What the agent reports: sorted check:record of each draft, or "run fails: <error>"."""
    ctx = AgentContext(
        study=_Reader(tables(spec)),
        clock=FixedClock(NOW),
        config=SaeReconcilerConfig(),
        log=structlog.get_logger(),
    )
    event: Event = trigger(CUT)
    assert isinstance(event.payload, DataCut)
    try:
        drafts = SaeReconciler().run(event, ctx)
    except Exception as exc:  # the stress set records a crash as an outcome
        return [f"run fails: {type(exc).__name__}"]
    return sorted(d.dedup_key.split("|")[5] for d in drafts)


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_stress_case_outcome_is_the_recorded_actual(path: Path) -> None:
    spec = load(path)
    assert VERSION in spec["actual"], f"{path.name}: run {VERSION} and record its actual"
    assert outcome(spec) == spec["actual"][VERSION]


def test_the_stress_set_covers_what_was_asked() -> None:
    names = {load(p)["case"] for p in CASES}
    assert len(CASES) >= 15
    assert {
        "recoded_term",
        "partial_onset_year_month",
        "partial_onset_year_only",
        "followup_same_case_id",
        "same_term_two_days_apart_wrong_date",
        "onset_three_days",
        "onset_four_days",
        "seriousness_criterion_differs",
        "subject_id_typo",
    } <= names

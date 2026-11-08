"""The development SAE stress set (tests/fixtures/sae_stress/): hand-written pairs, run
through safety.sae_reconciler as they are.

The test pins each case's `actual` outcome for the current agent version, recorded after
its run, so a change in behaviour shows up here; it does not test against the ideal. Where
ideal and actual differ, that is the backlog (docs/safety-eval.md). The sealed holdout set
(tests/fixtures/sae_stress_holdout/) is not run here.
"""

from pathlib import Path

import pytest

from meridian.agents.safety.sae_reconciler import VERSION
from tests.sae_stress_support import DEV, load, outcome

CASES = sorted(DEV.glob("*.json"))


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

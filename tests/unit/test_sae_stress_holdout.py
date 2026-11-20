"""The sealed SAE stress holdout (tests/fixtures/sae_stress_holdout/): judged once for
safety.sae_reconciler 1.1.0 (docs/safety-eval.md, "Sealed holdout").

Each case records the outcome of that single run as `actual` per version; this test pins
it, so later changes show up. The targets were checked on that run: every control meets
its ideal, at least 10 of the 12 other cases do, and no case raises.
"""

from pathlib import Path

import pytest

from meridian.agents.safety.sae_reconciler import MATCHING_VERSION as VERSION
from tests.sae_stress_support import HOLDOUT, load, outcome

CASES = sorted(HOLDOUT.glob("*.json"))


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_holdout_case_outcome_is_the_recorded_actual(path: Path) -> None:
    spec = load(path)
    assert VERSION in spec.get("actual", {}), f"{path.name}: no recorded run of {VERSION}"
    assert outcome(spec) == spec["actual"][VERSION]


def test_no_holdout_case_raises_and_every_control_meets_its_ideal() -> None:
    for path in CASES:
        spec = load(path)
        got = outcome(spec)
        assert not any(g.startswith("run fails") for g in got), path.name
        if spec["control"]:
            assert got == sorted(spec["ideal"]), path.name

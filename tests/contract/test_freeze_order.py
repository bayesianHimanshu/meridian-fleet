"""The AE variant freeze commit precedes every holdout 2 and AE holdout scorecard.

Checked on this checkout's own scorecards (data/evals) by the git commit each scorecard
recorded. Skipped where no such scorecard exists locally.
"""

from pathlib import Path

import pytest

from meridian.evals.report import ae_frozen_commit
from meridian.evals.scorecard import ScorecardStore, freeze_order_violations
from meridian.platform.eval_suite import step5_cuts

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_holdout2_scorecards_come_after_the_ae_freeze() -> None:
    holdout2, ae_holdouts = step5_cuts(REPO_ROOT / "config" / "scenarios")
    if holdout2 is None:
        pytest.skip("holdout 2 has not been generated")
    cuts = [holdout2[0], *(cut for cut, _ in ae_holdouts)]
    cards = [
        c for c in ScorecardStore(REPO_ROOT / "data" / "evals").all() if set(c.cuts) & set(cuts)
    ]
    if not cards:
        pytest.skip("no local scorecard scores holdout 2")
    sha = ae_frozen_commit(REPO_ROOT)
    assert len(sha) == 40, sha
    assert freeze_order_violations(cards, cuts, sha) == []

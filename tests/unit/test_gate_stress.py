"""The gate stress set (phase 5 step 3b): each case's outcome stays what was recorded when
the set was run once (scripts/gate_stress.py), and that was its ideal. A specification
test: the cases and the agent have the same author (docs/stage-gate-notes.md)."""

import json
from pathlib import Path

import pytest

from meridian.agents.portfolio.stage_gate_pack import VERSION
from meridian.portfolio.gate_cases import FIXTURES, outcome

CASES = sorted(FIXTURES.glob("*.json"))


def test_the_set_is_the_committed_one() -> None:
    assert len(CASES) == 10


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_each_case_is_what_was_recorded_and_ideal(path: Path) -> None:
    case = json.loads(path.read_text())
    assert outcome(case) == case["actual"][VERSION] == case["ideal"][VERSION]

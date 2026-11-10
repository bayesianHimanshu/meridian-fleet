"""The TMF stress set: each case's outcome must stay what was recorded when the set was run
once at the end of step 4a (tests/fixtures/tmf_stress/README.md). This pins behaviour so a
change shows up; it does not check against the ideal (docs/tmf-eval.md compares them)."""

from pathlib import Path

import pytest

from meridian.agents.quality.tmf_completeness import VERSION
from tests.tmf_stress_support import cases, load, outcome


def test_the_set_is_the_committed_one() -> None:
    assert len(cases()) == 15
    names = [load(p)["case"] for p in cases()]
    assert sum(n.startswith("clean_") for n in names) == 3


@pytest.mark.parametrize("path", cases(), ids=lambda p: p.stem)
def test_each_case_reports_what_was_recorded(path: Path) -> None:
    spec = load(path)
    assert outcome(spec) == spec["actual"][VERSION]

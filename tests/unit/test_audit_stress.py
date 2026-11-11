"""The audit stress set: each case's outcome must stay what was recorded when the set was
run once at the end of step 4b, for 1.1.0 once in phase 4a step 2 and for 1.2.0 once in
phase 4a step 3
(tests/fixtures/audit_stress/README.md). This pins
behaviour so a change shows up; docs/audit-eval.md compares it with the ideal."""

from pathlib import Path

import pytest

from meridian.agents.quality.audit_trail_reviewer import VERSION
from tests.audit_stress_support import cases, load, outcome


def test_the_set_is_the_committed_one() -> None:
    assert len(cases()) == 18  # 16 of step 4b, 2 of phase 4a step 2 (substrate runs)
    assert sum(load(p)["case"].startswith("clean_") for p in cases()) == 4


@pytest.mark.parametrize("path", cases(), ids=lambda p: p.stem)
def test_each_case_reports_what_was_recorded(path: Path) -> None:
    spec = load(path)
    assert outcome(spec) == spec["actual"][VERSION]

"""Run preclinical.tox_study_monitor once on the hand-written stress set (phase 5 step 1;
docs/preclinical-eval.md, "Stress set") and write each case's actual findings back into
its fixture under the agent version. It tests the specification: the cases and the agent
have the same author, so agreement shows the code follows the rules, not that the rules
are right. Usage: uv run python scripts/tox_stress.py"""

import json
from pathlib import Path

from meridian.agents.preclinical.tox_study_monitor import VERSION
from meridian.platform.config import Settings
from meridian.preclinical.inprocess import run_checks
from meridian.preclinical.synth import STUDY, StudySpec, build

FIXTURES = Path("tests/fixtures/tox_stress")


def main() -> None:
    run = run_checks(Settings().agents.tox_study_monitor, STUDY)
    agree = 0
    rows = []
    for path in sorted(FIXTURES.glob("*.json")):
        case = json.loads(path.read_text())
        actual = sorted(run(build(StudySpec.model_validate(case["spec"])), "TOX-STRESS"))
        ideal = case["ideal"][VERSION]
        same = actual == ideal
        agree += same
        missing = sorted(set(ideal) - set(actual))
        extra = sorted(set(actual) - set(ideal))
        rows.append((case["case"], same, missing, extra))
        case["actual"][VERSION] = actual
        path.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n")
    for name, same, missing, extra in rows:
        print(f"{'agree ' if same else 'DIFFER'} {name}")
        for m in missing:
            print(f"         missing {m}")
        for e in extra:
            print(f"         extra   {e}")
    print(f"{agree} of {len(rows)} cases as ideal ({VERSION})")


if __name__ == "__main__":
    main()

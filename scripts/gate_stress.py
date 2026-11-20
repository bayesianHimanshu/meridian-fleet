"""Run portfolio.stage_gate_pack's rules once on the hand-written stress set (phase 5 step
3b; docs/stage-gate-notes.md): each case's facts through meridian.portfolio.gates, its
outcome written back under the agent version. A specification test: the cases and the
agent have the same author. Usage: uv run python scripts/gate_stress.py"""

import json

from meridian.agents.portfolio.stage_gate_pack import VERSION
from meridian.portfolio.gate_cases import FIXTURES, outcome


def main() -> None:
    agree = 0
    paths = sorted(FIXTURES.glob("*.json"))
    for path in paths:
        case = json.loads(path.read_text())
        actual = outcome(case)
        ideal = case["ideal"][VERSION]
        same = actual == ideal
        agree += same
        case.setdefault("actual", {})[VERSION] = actual
        path.write_text(json.dumps(case, indent=2) + "\n")
        print(f"{'agree ' if same else 'DIFFER'} {case['name']}")
        if not same:
            print(f"         ideal  {json.dumps(ideal)}\n         actual {json.dumps(actual)}")
    print(f"{agree} of {len(paths)} cases as ideal (portfolio.stage_gate_pack {VERSION})")


if __name__ == "__main__":
    main()

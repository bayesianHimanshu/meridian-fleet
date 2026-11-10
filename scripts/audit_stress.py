"""Step 4b: run the hand-written audit stress set once and record what the agent reported.

Writes each case's outcome under "actual" for the current agent version and prints ideal
against actual. Run once, at the end of the step; the agent is not changed afterwards
(docs/audit-eval.md).

    PYTHONPATH=. uv run python scripts/audit_stress.py
"""

import json

from tests.audit_stress_support import cases, load, outcome

from meridian.agents.quality.audit_trail_reviewer import VERSION


def main() -> None:
    for path in cases():
        spec = load(path)
        got = outcome(spec)
        ideal = sorted(spec["ideal_findings"])
        spec["actual"] = {**spec.get("actual", {}), VERSION: got}
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        verdict = "match" if got == ideal else "DIFFERS"
        print(f"{path.stem}: {verdict}\n  ideal  {ideal}\n  actual {got}")


if __name__ == "__main__":
    main()

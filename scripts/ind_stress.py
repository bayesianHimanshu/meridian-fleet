"""Run preclinical.ind_readiness once on the hand-written stress set (phase 5 step 2;
docs/ind-readiness-eval.md, "Stress set") and write each case's actual findings and
percentage back into its fixture under the agent version. It tests the specification:
the cases and the agent have the same author, so agreement shows the code follows the
rules, not that the rules are what a regulatory reviewer would decide.
Usage: uv run python scripts/ind_stress.py"""

import json
from pathlib import Path

from meridian.agents.preclinical.ind_readiness import AGENT_ID, VERSION, IndReadiness
from meridian.platform.config import Settings
from meridian.preclinical.ind_cases import case_config, case_rows, evaluate, to_studies

FIXTURES = Path("tests/fixtures/ind_stress")


def main() -> None:
    base = Settings().agents.ind_readiness
    agent = IndReadiness()
    agree = 0
    rows = []
    for path in sorted(p for p in FIXTURES.glob("*.json") if not p.name.startswith("_")):
        case = json.loads(path.read_text())
        cfg = case_config(case, base)
        inventory = case_rows(case, FIXTURES)
        # The agent's own drafts, so the stress set runs the code that raises findings.
        drafts = agent.drafts(to_studies(inventory), cfg, "MRD-101", "INV-STRESS")
        findings = sorted(d.dedup_key.split("|")[-1] for d in drafts)
        percent = evaluate(inventory, cfg).percent_met
        ideal = case["ideal"][VERSION]
        same = findings == ideal["findings"] and percent == ideal["percent_met"]
        agree += same
        rows.append((case["name"], same, ideal, findings, percent))
        case.setdefault("actual", {})[VERSION] = {"findings": findings, "percent_met": percent}
        path.write_text(json.dumps(case, indent=2) + "\n")
    for name, same, ideal, findings, percent in rows:
        print(f"{'agree ' if same else 'DIFFER'} {name}")
        for m in sorted(set(ideal["findings"]) - set(findings)):
            print(f"         missing {m}")
        for e in sorted(set(findings) - set(ideal["findings"])):
            print(f"         extra   {e}")
        if percent != ideal["percent_met"]:
            print(f"         percent {percent} (ideal {ideal['percent_met']})")
    print(f"{agree} of {len(rows)} cases as ideal ({AGENT_ID} {VERSION})")


if __name__ == "__main__":
    main()

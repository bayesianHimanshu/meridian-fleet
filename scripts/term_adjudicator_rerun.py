"""Step 4a: safety.term_adjudicator 1.1.1 (validator 1.1.0: a reason of up to 2 sentences
and 300 characters) on the dev term pairs, against the 1.1.0 run of step 3b2.

Both pinned models answer every pair with the agent's prompt (unchanged), each in its own
jobs in the safety.local_model sandbox; the agreement rule combines them exactly as the
agent does. The 1.1.0 per-pair results are read from var/evals/term_agreement.json (written
by scripts/term_agreement_eval.py in step 3b2). Results go to
var/evals/term_adjudicator_1_1_1.json (not committed).

    uv run python scripts/term_adjudicator_rerun.py
"""

import argparse
import json
from pathlib import Path
from typing import Any

import structlog

from meridian.agents.safety import term_adjudicator as ta
from meridian.agents.safety import term_eval as tp
from meridian.platform.config import Settings
from meridian.platform.container import Container

BEFORE_VARIANT = "1.1.0 agreement"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", type=Path, default=Path("data/term_pairs/dev.jsonl"))
    parser.add_argument("--before", type=Path, default=Path("var/evals/term_agreement.json"))
    parser.add_argument("--out", type=Path, default=Path("var/evals/term_adjudicator_1_1_1.json"))
    args = parser.parse_args()
    pairs = tp.load_pairs(args.file)
    before: dict[str, Any] = json.loads(args.before.read_text())
    old = {r["id"]: r for r in before["results"][BEFORE_VARIANT]}
    old_summary = next(v for v in before["variants"] if v["variant"] == BEFORE_VARIANT)
    with Container.build(Settings()) as c:
        log: structlog.typing.FilteringBoundLogger = structlog.get_logger()
        local = c.local_model_settings()
        reports = [
            tp.run_model(pairs, local, c.local_model_executor(log), model, ta.SYSTEM_PROMPT)
            for model in local.adjudicator_models
        ]
    both = tp.agreement(reports)
    summary = {"variant": f"{ta.VERSION} agreement", **both.summary()}
    print(json.dumps({"before": old_summary, "after": summary}, indent=2, sort_keys=True))
    changed = []
    for x in both.results:
        o = old[x.pair.id]
        if (o["status"], o["verdict"]) != (x.status, x.verdict):
            changed.append(
                {
                    "id": x.pair.id,
                    "label": x.pair.label,
                    "before": o["verdict"] or ",".join(o["rejection_reasons"]),
                    "after": x.verdict or ",".join(x.rejection_reasons),
                }
            )
    for r in reports:
        rejected = [(x.pair.id, x.rejection_reasons) for x in r.results if x.status == "rejected"]
        print(f"{r.model}: rejected {rejected}")
    for ch in changed:
        print(f"  changed {ch}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "before": old_summary,
                "after": summary,
                "changed": changed,
                "per_model": [r.summary() for r in reports],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

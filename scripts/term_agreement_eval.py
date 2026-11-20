"""Step 3b2: term adjudicator 1.0.0, the agreement rule (1.1.0) and the unsure prompt on
the dev term pairs (docs/local-model-eval.md, "abstain on disagreement").

Runs four (model, prompt) combinations, each in its own jobs in the safety.local_model
sandbox: phi4-mini and qwen3.5 with the 1.0.0 prompt, and both with the unsure prompt.
The variants are combined from them (agreement is computed exactly as the agent
combines answers). Per-pair results go to var/evals/term_agreement.json (not committed).

    uv run python scripts/term_agreement_eval.py [--file data/term_pairs/dev.jsonl]
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

PROMPTS = {"1.0.0": ta.SYSTEM_PROMPT, "unsure": ta.UNSURE_PROMPT}


def row(name: str, r: tp.ModelReport, base: tp.ModelReport | None) -> dict[str, Any]:
    out: dict[str, Any] = {"variant": name, **r.summary()}
    if base is not None and base is not r:
        cost = tp.abstention_cost(base, r)
        out |= {"lost_vs_1_0_0": list(cost.lost), "gained_vs_1_0_0": list(cost.gained)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", type=Path, default=Path("data/term_pairs/dev.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("var/evals/term_agreement.json"))
    args = parser.parse_args()
    pairs = tp.load_pairs(args.file)
    with Container.build(Settings()) as c:
        log: structlog.typing.FilteringBoundLogger = structlog.get_logger()
        local = c.local_model_settings()
        first, second = local.adjudicator_models
        runs: dict[tuple[str, str], tp.ModelReport] = {}
        for prompt in PROMPTS:
            for model in (first, second):
                report = tp.run_model(
                    pairs, local, c.local_model_executor(log), model, PROMPTS[prompt]
                )
                report.placement = tp.placement(local.base_url, model)
                runs[(model, prompt)] = report
                print(f"ran {model} with the {prompt} prompt: {report.batch_error or 'ok'}")
    base = runs[(first, "1.0.0")]
    variants = {
        f"1.0.0 ({first})": base,
        f"{second} alone": runs[(second, "1.0.0")],
        "1.1.0 agreement": tp.agreement([base, runs[(second, "1.0.0")]]),
        f"unsure prompt ({first})": runs[(first, "unsure")],
        f"unsure prompt ({second})": runs[(second, "unsure")],
        "agreement + unsure prompt": tp.agreement(
            [runs[(first, "unsure")], runs[(second, "unsure")]]
        ),
    }
    rows = [row(name, r, base) for name, r in variants.items()]
    for summary in rows:
        print(json.dumps(summary, sort_keys=True))
    for name, r in variants.items():
        for x in r.results:
            if not x.correct:
                print(
                    f"  {name}: {x.pair.id} {x.pair.category} label {x.pair.label}: "
                    f"{x.status} {x.verdict or ','.join(x.rejection_reasons) or x.error}"
                )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "file": args.file.as_posix(),
                "variants": rows,
                "results": {
                    name: [
                        {
                            "id": x.pair.id,
                            "label": x.pair.label,
                            "category": x.pair.category,
                            "status": x.status,
                            "verdict": x.verdict,
                            "reason": x.reason,
                            "rejection_reasons": list(x.rejection_reasons),
                            "latency_ms": x.latency_ms,
                        }
                        for x in r.results
                    ]
                    for name, r in variants.items()
                },
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

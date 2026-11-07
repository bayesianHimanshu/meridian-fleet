"""Validator 1.0.0 and 1.1.0 on data/narrator_eval/labelled.jsonl, side by side.

    uv run python scripts/narrator_validator_eval.py

Prints markdown for docs/narrator-eval.md: recall on bad (overall, per origin, per
mutation type), false rejections on real_good, and whether each rejection gave the
expected reason. Deterministic; no model calls.
"""

from collections import Counter
from pathlib import Path

from meridian.agents.rbqm.narrator_eval import (
    MUTATIONS,
    VALIDATORS,
    LabelledRecord,
    Metrics,
    evaluate,
)

LABELLED = Path("data/narrator_eval/labelled.jsonl")


def main() -> None:
    records = [
        LabelledRecord.model_validate_json(line)
        for line in LABELLED.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results: dict[str, Metrics] = {name: evaluate(records, v) for name, v in VALIDATORS.items()}
    names = list(results)
    counts = Counter(r.origin for r in records)
    print(
        f"{len(records)} records: real_good {counts['real_good']}, real_bad "
        f"{counts['real_bad']}, mutated {counts['mutated']}.\n"
    )
    print("| Metric | " + " | ".join(f"validator {n}" for n in names) + " |")
    print("| --- |" + " ---: |" * len(names))

    def row(label: str, cells: list[str]) -> None:
        print(f"| {label} | " + " | ".join(cells) + " |")

    row("recall on bad, overall", [str(results[n].recall) for n in names])
    for origin in ("real_bad", "mutated"):
        row(f"recall, {origin}", [str(results[n].recall_by_origin[origin]) for n in names])
    for mutation in MUTATIONS:
        row(f"recall, {mutation}", [str(results[n].recall_by_mutation[mutation]) for n in names])
    for mutation in MUTATIONS:
        row(
            f"recall, {mutation}, base accepted",
            [str(results[n].recall_by_mutation_clean_base[mutation]) for n in names],
        )
    row(
        "false rejections on real_good",
        [f"{len(results[n].false_rejections)} of {results[n].good_total}" for n in names],
    )
    outcomes = (
        "rejected for the expected reason",
        "rejected for another reason only",
        "rejected (no expected code)",
        "accepted",
    )
    for outcome in outcomes:
        row(f"bad: {outcome}", [str(results[n].expected_reason[outcome]) for n in names])

    for n in names:
        m = results[n]
        print(f"\n### Validator {n}: real_bad\n")
        for v in m.verdicts:
            if v.record.origin == "real_bad":
                expected = ", ".join(v.record.expected_reasons) or "none"
                got = ", ".join(v.codes) or "accepted"
                print(f"* {v.record.finding_id}: expected {expected}; got {got}")
        print(f"\n### Validator {n}: false rejections on real_good\n")
        reasons = Counter(c for v in m.false_rejections for c in v.codes)
        print("By code: " + (", ".join(f"{c} {k}" for c, k in reasons.most_common()) or "none"))
        for v in m.false_rejections:
            print(f"* {v.record.finding_id}: {', '.join(v.codes)}")
        print(f"\n### Validator {n}: expected code against codes given (bad records)\n")
        print("| Expected | Given | Records |\n| --- | --- | ---: |")
        for (expected, got), k in sorted(m.confusion.items()):
            print(f"| {expected} | {got} | {k} |")


if __name__ == "__main__":
    main()

"""The gate stress set's outcome of one case (phase 5 step 3b), shared by
scripts/gate_stress.py (run once) and its test."""

from pathlib import Path

from meridian.portfolio.facts import from_rows
from meridian.portfolio.gates import evaluate, load_gates

FIXTURES = Path("tests/fixtures/gate_stress")


def outcome(case: dict[str, object]) -> dict[str, object]:
    gate = load_gates().get(str(case["asset_id"]), str(case["gate"]))
    if gate is None:
        raise ValueError(f"no gate {case['gate']} for {case['asset_id']}")
    rows = case["facts"]
    assert isinstance(rows, list)
    results = evaluate(gate, from_rows(rows))
    return {
        "criteria": {r.criterion.id: r.status for r in results},
        "open_items": sum(len(r.open_items) for r in results),
    }

"""Print whether the step 4 detector versions flag what the previous ones did, per cut.

    uv run python scripts/detector_equivalence.py

See meridian.evals.detector_equivalence. Exits 1 on any difference.
"""

import sys
from pathlib import Path

from meridian.evals.detector_equivalence import compare
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import SystemClock
from meridian.platform.config import Settings


def main() -> int:
    s = Settings()
    ledger = SqliteLedger(
        path=Path(s.ledger_path), clock=SystemClock(), finding_id_prefixes=s.finding_id_prefixes
    )
    result = compare(ledger, s.study_id)
    ledger.close()
    print(
        f"cuts {result.cuts}; (agent, cut) pairs compared {result.pairs}; findings "
        f"{result.findings_old} before, {result.findings_new} after; "
        f"differences {len(result.differences)}"
    )
    for line in result.differences:
        print("  " + line)
    print("equivalent" if result.equivalent else "NOT EQUIVALENT")
    return 0 if result.equivalent else 1


if __name__ == "__main__":
    sys.exit(main())

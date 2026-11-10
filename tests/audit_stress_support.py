"""The hand-written audit stress set (tests/fixtures/audit_stress): each case's rows, with
the two shorthands of its README expanded, run through the agent. Nothing else of ours
touches the rows."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from meridian.agents.quality.audit_trail_reviewer import review
from meridian.domain import audit as a
from meridian.ledger.hashing import GENESIS_HASH
from meridian.platform.agent_settings import AuditTrailReviewerConfig

ROOT = Path(__file__).resolve().parent / "fixtures" / "audit_stress"
_SHORT = re.compile(r"h(\d+)")


def cases() -> list[Path]:
    return sorted(ROOT.glob("[0-9][0-9]_*.json"))


def load(path: Path) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(path.read_text())
    return spec


def _hex(value: str) -> str:
    m = _SHORT.fullmatch(value)
    return f"{int(m.group(1)):064x}" if m else value


def table(spec: dict[str, Any]) -> pl.DataFrame:
    rows = []
    prev = GENESIS_HASH
    for given in spec["rows"]:
        row: dict[str, Any] = dict.fromkeys(a.COLUMNS)
        row |= {a.REDACTED: "N", **given}
        row[a.HASH] = _hex(str(given.get(a.HASH, f"h{given[a.SEQ]}")))
        row[a.PREV_HASH] = _hex(str(given.get(a.PREV_HASH, prev)))
        prev = row[a.HASH]
        rows.append(row)
    schema = {c: (pl.Int64 if c in a.INT_COLUMNS else pl.String) for c in a.COLUMNS}
    return pl.DataFrame(rows, schema=schema, orient="row")


def outcome(spec: dict[str, Any]) -> list[str]:
    """Sorted check:record of what the agent reports, or "run fails: <error>". The review
    happens a day after the last row (no run is still in progress)."""
    t = table(spec)
    last = max(datetime.fromisoformat(r[a.RECORDED_AT]) for r in spec["rows"])
    now = last + timedelta(days=1)
    try:
        hits = review(t, 1, spec["through_seq"], AuditTrailReviewerConfig(), now)
    except Exception as exc:  # the stress set records a crash as an outcome
        return [f"run fails: {type(exc).__name__}"]
    return sorted(f"{h.check}:{h.record}" for h in hits)

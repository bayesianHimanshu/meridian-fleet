"""The portfolio facts (phase 5 step 3b; docs/stage-gate-notes.md): one row per fact the
portfolio lead may see, each with its ledger seq (none for a static fact). Built by the
host (meridian.api.portfolio_facts) and staged as /stage/portfolio; read by
portfolio.pipeline_intelligence and portfolio.stage_gate_pack. Pure: no ledger here."""

from collections.abc import Sequence
from dataclasses import asdict, dataclass

import polars as pl

FACTS_TABLE = "portfolio_facts"
PREVIOUS_TABLE = "previous_facts"  # the previous outlook's facts (outlook_seq on each row)
CLOSED = frozenset({"accepted", "dismissed"})
OPEN = frozenset({"open", "escalated"})


@dataclass(frozen=True, slots=True)
class Fact:
    asset_id: str
    group: str
    key: str
    label: str
    value: str = ""
    status: str = ""
    severity: str | None = None
    seq: int | None = None
    finding_id: str | None = None
    number: float | None = None
    static: bool = False


SCHEMA: dict[str, type[pl.DataType] | pl.DataType] = {
    "asset_id": pl.String,
    "group": pl.String,
    "key": pl.String,
    "label": pl.String,
    "value": pl.String,
    "status": pl.String,
    "severity": pl.String,
    "seq": pl.Int64,
    "finding_id": pl.String,
    "number": pl.Float64,
    "static": pl.Boolean,
}


def frame(facts: Sequence[Fact]) -> pl.DataFrame:
    rows = [asdict(f) for f in facts]
    return pl.DataFrame(rows, schema=SCHEMA, orient="row") if rows else pl.DataFrame(schema=SCHEMA)


def from_frame(table: pl.DataFrame) -> list[Fact]:
    names = set(SCHEMA)
    return [Fact(**{k: v for k, v in r.items() if k in names}) for r in table.iter_rows(named=True)]


def from_rows(rows: Sequence[dict[str, object]]) -> list[Fact]:
    """From JSON rows (the stress set), with the same columns."""
    return from_frame(pl.DataFrame(list(rows), schema=SCHEMA, orient="row"))

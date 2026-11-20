"""The single definition of what must never appear in blinded or ops data."""

from collections.abc import Iterable

import polars as pl

FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "ARM",
        "ARMCD",
        "ACTARM",
        "ACTARMCD",
        "EXTRT",
        "EXDOSE",
        "EXDOSU",
        "EXDOSFRM",
        "TRT01P",
        "TRT01A",
        "TRTP",
        "TRTA",
    }
)


class BlindingViolationError(Exception):
    """Raised when a forbidden column is about to reach a blinded or ops area."""


def forbidden_columns_in(columns: Iterable[str]) -> list[str]:
    """Columns whose name contains any forbidden name, case-insensitively.

    Containment, not equality, so derived names such as ARMCD_DATE or SRC_ARM are caught.
    """
    return [c for c in columns if any(name in c.upper() for name in FORBIDDEN_COLUMNS)]


def assert_blinded(table: str, frame: pl.DataFrame) -> None:
    bad = forbidden_columns_in(frame.columns)
    if bad:
        raise BlindingViolationError(
            f"table {table!r} contains forbidden column(s) {sorted(bad)}; "
            "they may only be written to unblinded/"
        )

"""The only module that touches SAS transport (XPT) files."""

from pathlib import Path

import polars as pl
import pyreadstat


def read_xpt(path: Path) -> pl.DataFrame:
    """Read one XPT file. Column names are upper-cased, as SDTM expects."""
    frame, _meta = pyreadstat.read_xport(str(path), output_format="polars")
    if not isinstance(frame, pl.DataFrame):
        raise TypeError(f"pyreadstat returned {type(frame).__name__} for {path}")
    return frame.rename({c: c.upper() for c in frame.columns})

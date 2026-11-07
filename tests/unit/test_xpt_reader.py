from pathlib import Path

import polars as pl
import pyreadstat

from meridian.data.xpt_reader import read_xpt


def test_round_trip_and_upper_cases_columns(tmp_path: Path) -> None:
    source = pl.DataFrame({"usubjid": ["01-701-1015", "01-701-1023"], "AGE": [63.0, None]})
    path = tmp_path / "dm.xpt"
    pyreadstat.write_xport(source, str(path), table_name="DM")
    frame = read_xpt(path)
    assert frame.columns == ["USUBJID", "AGE"]
    assert frame["USUBJID"].to_list() == ["01-701-1015", "01-701-1023"]
    assert frame["AGE"].to_list() == [63.0, None]

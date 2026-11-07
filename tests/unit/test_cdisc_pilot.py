import datetime as dt

import polars as pl
import pytest

from meridian.data.blinding import FORBIDDEN_COLUMNS, forbidden_columns_in
from meridian.data.cdisc_pilot import CuratedStudy, TransformError, add_parsed_dates, curate
from tests.data_support import SITE_MAP, pilot_frames


@pytest.fixture
def curated() -> CuratedStudy:
    return curate(pilot_frames(), SITE_MAP, "MRD-201")


def _all(c: CuratedStudy) -> dict[str, pl.DataFrame]:
    return {
        **{f"blinded/{k}": v for k, v in c.blinded.items()},
        **{f"unblinded/{k}": v for k, v in c.unblinded.items()},
    }


def test_table_sets(curated: CuratedStudy) -> None:
    assert set(curated.blinded) == {"dm", "ae", "vs", "lb", "ds", "sv", "ex_dates"}
    assert set(curated.unblinded) == {"randomization", "ex"}


def test_forbidden_columns_never_reach_blinded(curated: CuratedStudy) -> None:
    for name, frame in curated.blinded.items():
        assert forbidden_columns_in(frame.columns) == [], name
    assert {"ARMCD", "ARM", "ACTARMCD", "ACTARM"} <= set(curated.unblinded["randomization"].columns)
    assert {"EXTRT", "EXDOSE", "EXDOSU", "EXDOSFRM"} <= set(curated.unblinded["ex"].columns)


def test_ex_dates_has_exactly_the_safe_columns(curated: CuratedStudy) -> None:
    assert curated.blinded["ex_dates"].columns == [
        "STUDYID",
        "USUBJID",
        "SRC_USUBJID",
        "EXSEQ",
        "VISITNUM",
        "VISIT",
        "EXSTDTC",
        "EXSTDTC_DATE",
        "EXENDTC",
        "EXENDTC_DATE",
    ]


def test_randomization_columns(curated: CuratedStudy) -> None:
    assert curated.unblinded["randomization"].columns == [
        "STUDYID",
        "USUBJID",
        "SRC_USUBJID",
        "ARMCD",
        "ARM",
        "ACTARMCD",
        "ACTARM",
    ]


def test_studyid_relabelled_everywhere(curated: CuratedStudy) -> None:
    for name, frame in _all(curated).items():
        assert frame["STUDYID"].unique().to_list() == ["MRD-201"], name


def test_usubjid_numbered_within_site_by_original_id(curated: CuratedStudy) -> None:
    mapping = dict(
        zip(curated.subject_map["SRC_USUBJID"], curated.subject_map["USUBJID"], strict=True)
    )
    assert mapping == {
        "01-701-1002": "201-1001-001",
        "01-701-1015": "201-1001-002",
        "01-701-1023": "201-1001-003",
        "01-703-1050": "201-1003-001",
        "01-703-1100": "201-1003-002",
    }


def test_usubjid_mapping_consistent_and_src_kept_everywhere(curated: CuratedStudy) -> None:
    expected = dict(
        zip(curated.subject_map["SRC_USUBJID"], curated.subject_map["USUBJID"], strict=True)
    )
    for name, frame in _all(curated).items():
        assert "SRC_USUBJID" in frame.columns, name
        pairs = dict(zip(frame["SRC_USUBJID"], frame["USUBJID"], strict=True))
        assert all(expected[src] == new for src, new in pairs.items()), name


def test_dm_sites_relabelled_with_lineage(curated: CuratedStudy) -> None:
    dm = curated.blinded["dm"]
    rows = dm.select("USUBJID", "SUBJID", "SITEID", "SRC_SITEID").rows()
    assert rows[0] == ("201-1001-001", "001", "1001", "701")
    assert set(dm["SITEID"]) == {"1001", "1003"}


def test_partial_dates_keep_rows_and_leave_parsed_null(curated: CuratedStudy) -> None:
    ae = curated.blinded["ae"]
    assert ae.height == 4
    by_raw = dict(zip(ae["AESTDTC"], ae["AESTDTC_DATE"], strict=True))
    assert by_raw["2014-01-05"] == dt.date(2014, 1, 5)
    assert by_raw["2014-02"] is None
    assert by_raw["2014"] is None
    assert by_raw[""] is None
    assert ae.schema["AESTDTC_DATE"] == pl.Date
    assert ae.schema["AESTDTC"] == pl.String


def test_datetime_values_parse_their_date_part(curated: CuratedStudy) -> None:
    dm = curated.blinded["dm"]
    by_raw = dict(zip(dm["RFPENDTC"], dm["RFPENDTC_DATE"], strict=True))
    assert by_raw["2014-07-02T11:45"] == dt.date(2014, 7, 2)
    assert by_raw["2013-01"] is None


def test_invalid_calendar_date_is_null_not_an_error() -> None:
    frame = pl.DataFrame({"XXDTC": ["2014-02-30", "2014-02-28"]})
    assert add_parsed_dates(frame)["XXDTC_DATE"].to_list() == [None, dt.date(2014, 2, 28)]


def test_parsed_column_sits_after_its_source() -> None:
    frame = pl.DataFrame({"A": [1], "XXDTC": ["2014-01-01"], "B": [2]})
    assert add_parsed_dates(frame).columns == ["A", "XXDTC", "XXDTC_DATE", "B"]


def test_unknown_subject_is_rejected() -> None:
    frames = pilot_frames()
    frames["ae"] = frames["ae"].with_columns(pl.lit("01-799-9999").alias("USUBJID"))
    with pytest.raises(TransformError, match="not in dm"):
        curate(frames, SITE_MAP, "MRD-201")


def test_site_missing_from_map_is_rejected() -> None:
    with pytest.raises(TransformError, match="not in the site map"):
        curate(pilot_frames(), {"701": "1001"}, "MRD-201")


def test_missing_domain_is_rejected() -> None:
    frames = pilot_frames()
    del frames["lb"]
    with pytest.raises(TransformError, match="lb"):
        curate(frames, SITE_MAP, "MRD-201")


def test_forbidden_set_is_the_specified_one() -> None:
    assert (
        frozenset(
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
        == FORBIDDEN_COLUMNS
    )

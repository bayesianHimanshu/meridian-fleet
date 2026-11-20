"""SEND ingest (phase 5 step 1): relabelling, determinism, append-only cuts and events."""

import json
from pathlib import Path

import polars as pl
import pytest

from meridian.data.cut_writer import CutExistsError, SourceFile
from meridian.data.fetch import MANIFEST_NAME
from meridian.data.files import sha256_file
from meridian.data.send import (
    SendIngestError,
    check_pinned,
    curate,
    record_send_study,
    send_study_events,
    write_send_cut,
)
from meridian.data.study_store import SendStudyReader, StudyAccessError
from meridian.platform.clock import FixedClock
from tests.support import T0, make_ledger

SOURCES = [SourceFile(name="dm.xpt", url="https://example.org/dm.xpt", sha256="0" * 64)]


def frames() -> dict[str, pl.DataFrame]:
    dm = pl.DataFrame(
        {
            "STUDYID": ["PC201708"] * 3,
            "USUBJID": ["PC201708-1002", "PC201708-1001", "PC201708-4001"],
            "SUBJID": ["1002", "1001", "4001"],
            "SEX": ["M", "M", "F"],
            "ARMCD": ["1", "1", "4"],
            "ARM": ["Vehicle Control", "Vehicle Control", "200 mg/kg PCDRUG"],
            "SETCD": ["1", "1", "4"],
        }
    )
    ts = pl.DataFrame(
        {
            "STUDYID": ["PC201708"] * 2,
            "TSSEQ": [2.0, 1.0],
            "TSPARMCD": ["TRT", "STITLE"],
            "TSVAL": ["PCDRUG", "13-Week Study on PCDRUG (PC201708)"],
        }
    )
    lb = pl.DataFrame(
        {
            "STUDYID": ["PC201708"] * 2,
            "USUBJID": ["PC201708-1001", "PC201708-1001"],
            "LBSEQ": [2.0, 1.0],
            "LBTESTCD": ["AST", "ALT"],
            "LBSTRESN": [80.0, 40.0],
        }
    )
    return {"dm": dm, "ts": ts, "lb": lb}


def curated() -> dict[str, pl.DataFrame]:
    return curate(
        frames(),
        study_id="MRD-101-TOX-01",
        source_study_id="PC201708",
        treatment="MRD-101",
        source_treatment="PCDRUG",
    )


def test_curation_relabels_study_and_treatment_everywhere_and_keeps_the_source_id() -> None:
    out = curated()
    assert out["dm"]["USUBJID"].to_list() == [
        "MRD-101-TOX-01-1001", "MRD-101-TOX-01-1002", "MRD-101-TOX-01-4001",
    ]  # fmt: skip
    assert out["dm"]["SRC_USUBJID"].to_list() == [
        "PC201708-1001", "PC201708-1002", "PC201708-4001",
    ]  # fmt: skip
    assert out["dm"]["ARM"].to_list()[-1] == "200 mg/kg MRD-101"
    assert out["ts"]["TSVAL"].to_list() == ["13-Week Study on MRD-101 (MRD-101-TOX-01)", "MRD-101"]
    assert out["lb"]["LBTESTCD"].to_list() == ["ALT", "AST"]  # sorted by USUBJID, LBSEQ
    for name, frame in out.items():
        for col, dtype in frame.schema.items():
            if dtype == pl.String and col != "SRC_USUBJID":
                assert not frame[col].str.contains("PCDRUG|PC201708").any(), (name, col)


def test_curation_refuses_an_unknown_domain() -> None:
    with pytest.raises(SendIngestError):
        curate({"xx": pl.DataFrame({"A": [1]})}, study_id="S", source_study_id="P",
               treatment="T", source_treatment="Q")  # fmt: skip


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_re_ingest_is_byte_identical_and_appends_nothing(tmp_path: Path) -> None:
    results = []
    for name in ("one", "two"):
        ledger = make_ledger(tmp_path / f"{name}.sqlite3", FixedClock(T0))
        for _ in range(2):
            cut = write_send_cut(
                send_root=tmp_path / name, study_id="MRD-101-TOX-01", cut_id="TOX-FINAL",
                tables=curated(), created_at=T0, sources=SOURCES,
            )  # fmt: skip
            _event, appended = record_send_study(
                ledger, occurred_at=T0, actor="system.ingest", study_id="MRD-101-TOX-01",
                cut_id="TOX-FINAL", path=cut.path,
            )  # fmt: skip
            results.append((name, cut.created, appended))
        assert len(send_study_events(ledger)) == 1
    assert results == [
        ("one", True, True), ("one", False, False), ("two", True, True), ("two", False, False),
    ]  # fmt: skip
    one, two = _files(tmp_path / "one"), _files(tmp_path / "two")
    assert [p.relative_to(tmp_path / "one") for p in one] == [
        p.relative_to(tmp_path / "two") for p in two
    ]
    assert all(a.read_bytes() == b.read_bytes() for a, b in zip(one, two, strict=True))


def test_a_cut_with_other_content_is_refused(tmp_path: Path) -> None:
    write_send_cut(send_root=tmp_path, study_id="S", cut_id="C", tables=curated(),
                   created_at=T0, sources=SOURCES)  # fmt: skip
    changed = {**curated(), "lb": curated()["lb"].head(1)}
    with pytest.raises(CutExistsError):
        write_send_cut(send_root=tmp_path, study_id="S", cut_id="C", tables=changed,
                       created_at=T0, sources=SOURCES)  # fmt: skip


def test_the_send_reader_sees_only_the_send_area(tmp_path: Path) -> None:
    cut = write_send_cut(send_root=tmp_path, study_id="S", cut_id="C", tables=curated(),
                         created_at=T0, sources=SOURCES)  # fmt: skip
    (cut.path / "blinded").mkdir()
    pl.DataFrame({"X": [1]}).write_parquet(cut.path / "blinded" / "ae.parquet")
    reader = SendStudyReader(tmp_path / "S", "C")
    assert reader.study_id == "S"
    assert reader.tables() == ["dm", "lb", "ts"]
    with pytest.raises(KeyError):
        reader.table("ae")
    with pytest.raises(StudyAccessError):
        reader.table("../blinded/ae")


def test_ingest_refuses_a_raw_file_that_is_not_the_pinned_one(tmp_path: Path) -> None:
    (tmp_path / "dm.xpt").write_bytes(b"not the pinned file")
    actual = sha256_file(tmp_path / "dm.xpt")
    entry = {"url": "u", "sha256": actual, "size_bytes": 19, "fetched_at": T0.isoformat()}
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"files": {"dm.xpt": entry}}))
    assert check_pinned(tmp_path, {"dm": actual})[0].sha256 == actual
    with pytest.raises(SendIngestError, match="refusing to ingest"):
        check_pinned(tmp_path, {"dm": "f" * 64})
    with pytest.raises(SendIngestError, match="fetch first"):
        check_pinned(tmp_path, {"lb": actual})

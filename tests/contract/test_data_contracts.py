"""Data-layer guarantees: blinding on disk, reader confinement, atomic and repeatable cuts."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from meridian.data.blinding import forbidden_columns_in
from meridian.data.cut_writer import (
    CutExistsError,
    CutManifest,
    ParquetWriter,
    write_parquet_deterministic,
)
from meridian.data.files import sha256_file
from meridian.data.ingest import IngestResult
from meridian.data.study_store import ParquetStudyReader, StudyAccessError, UnblindedReader
from meridian.domain.enums import EventType
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from tests.data_support import SOURCES, make_ingest, pilot_frames
from tests.support import T0, make_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
CUT = "DC-BASE"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[SqliteLedger]:
    with make_ledger(tmp_path / "ledger.sqlite3", FixedClock(T0)) as opened:
        yield opened


def _ingest(root: Path, ledger: SqliteLedger, seed: int = 20261107) -> IngestResult:
    service = make_ingest(root, FixedClock(T0), ledger)
    return service.ingest_frames(pilot_frames(), cut_id=CUT, seed=seed, sources=SOURCES)


@pytest.fixture
def cut(tmp_path: Path, ledger: SqliteLedger) -> Path:
    return _ingest(tmp_path, ledger).cut_path


def _parquet_in_protected_areas(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.parquet") if {"blinded", "ops"} & set(p.relative_to(root).parts)
    )


def test_no_forbidden_column_in_any_blinded_or_ops_parquet(cut: Path) -> None:
    roots = [cut.parent.parent]
    repo_curated = REPO_ROOT / "data" / "curated"
    if repo_curated.exists():
        roots.append(repo_curated)
    checked = 0
    for root in roots:
        for path in _parquet_in_protected_areas(root):
            assert forbidden_columns_in(pl.read_parquet_schema(path)) == [], path
            checked += 1
    assert checked >= 11


def test_manifest_lists_every_file_with_hash_rows_and_columns(cut: Path) -> None:
    manifest = CutManifest.load(cut)
    on_disk = sorted(str(p.relative_to(cut)) for p in cut.rglob("*.parquet"))
    assert sorted(f.path for f in manifest.files) == on_disk
    for entry in manifest.files:
        frame = pl.read_parquet(cut / entry.path)
        assert entry.sha256 == sha256_file(cut / entry.path)
        assert entry.rows == frame.height
        assert list(entry.columns) == frame.columns
    assert [s.name for s in manifest.sources] == sorted(s.name for s in SOURCES)
    assert manifest.seed_record["spawn_order"] == [
        "data_entry",
        "queries",
        "deviations",
        "screening",
    ]


# Reader confinement


@pytest.fixture
def reader(cut: Path) -> ParquetStudyReader:
    return ParquetStudyReader(cut.parent, CUT)


def test_reader_sees_only_blinded_and_ops(reader: ParquetStudyReader) -> None:
    assert reader.study_id == "MRD-201"
    assert reader.cut_id == CUT
    assert reader.tables() == [
        "ae",
        "dm",
        "ds",
        "ex_dates",
        "lb",
        "sv",
        "vs",
        "data_entry",
        "deviations",
        "queries",
        "screening",
    ]
    assert reader.table("dm").height == 5


@pytest.mark.parametrize("name", ["randomization", "ex"])
def test_reader_cannot_reach_unblinded_tables(reader: ParquetStudyReader, name: str) -> None:
    with pytest.raises(KeyError):
        reader.table(name)


@pytest.mark.parametrize(
    "name",
    [
        "../unblinded/ex",
        "../unblinded/randomization",
        "unblinded/ex",
        "..",
        "../../../ground_truth/answers",
        "/etc/passwd",
        str(Path("/tmp/dm")),
        "DM",
        "dm.parquet",
        "",
    ],
)
def test_reader_refuses_paths(reader: ParquetStudyReader, name: str) -> None:
    with pytest.raises(StudyAccessError):
        reader.table(name)


def test_reader_refuses_ground_truth_even_if_it_exists(cut: Path) -> None:
    truth = cut.parent.parent.parent / "ground_truth"
    truth.mkdir()
    pl.DataFrame({"x": [1]}).write_parquet(truth / "answers.parquet")
    reader = ParquetStudyReader(cut.parent, CUT)
    with pytest.raises(StudyAccessError):
        reader.table("../../../ground_truth/answers")
    assert "answers" not in reader.tables()


def test_reader_refuses_an_outward_symlink(cut: Path) -> None:
    os.symlink(cut / "unblinded" / "randomization.parquet", cut / "blinded" / "sneaky.parquet")
    reader = ParquetStudyReader(cut.parent, CUT)
    with pytest.raises(StudyAccessError, match="outside"):
        reader.table("sneaky")
    assert "sneaky" not in reader.tables()


def test_reader_refuses_a_symlinked_area(cut: Path) -> None:
    shutil.rmtree(cut / "ops")
    os.symlink(cut / "unblinded", cut / "ops")
    reader = ParquetStudyReader(cut.parent, CUT)
    with pytest.raises(StudyAccessError):
        reader.table("randomization")
    assert "randomization" not in reader.tables()


def test_reader_refuses_a_bad_cut_id(cut: Path) -> None:
    for bad in ("../MRD-201/DC-BASE", "/abs", ".."):
        with pytest.raises(StudyAccessError):
            ParquetStudyReader(cut.parent, bad)


def test_unblinded_reader_is_separate_and_limited(cut: Path) -> None:
    unblinded = UnblindedReader(cut.parent, CUT)
    assert unblinded.tables() == ["ex", "randomization"]
    with pytest.raises(KeyError):
        unblinded.table("dm")


# Atomicity, determinism, idempotency


class _CrashError(RuntimeError):
    pass


def _crashing_writer(after: int) -> ParquetWriter:
    count = {"n": 0}

    def writer(frame: pl.DataFrame, path: Path) -> None:
        count["n"] += 1
        if count["n"] > after:
            raise _CrashError("simulated crash mid-write")
        write_parquet_deterministic(frame, path)

    return writer


def test_crash_mid_write_leaves_no_cut_under_final_name(
    tmp_path: Path, ledger: SqliteLedger
) -> None:
    service = make_ingest(tmp_path, FixedClock(T0), ledger, writer=_crashing_writer(after=5))
    with pytest.raises(_CrashError):
        service.ingest_frames(pilot_frames(), cut_id=CUT, seed=1, sources=SOURCES)
    study = tmp_path / "curated" / "MRD-201"
    assert not (study / CUT).exists()
    assert list(study.iterdir()) == []
    assert ledger.read(event_types=[EventType.DATA_CUT]) == []


def test_hard_crash_leftovers_never_masquerade_as_a_cut(
    tmp_path: Path, ledger: SqliteLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real crash (power loss) skips cleanup. Simulate that by disabling rmtree.
    monkeypatch.setattr(shutil, "rmtree", lambda *_a, **_k: None)
    service = make_ingest(tmp_path, FixedClock(T0), ledger, writer=_crashing_writer(after=5))
    with pytest.raises(_CrashError):
        service.ingest_frames(pilot_frames(), cut_id=CUT, seed=1, sources=SOURCES)
    monkeypatch.undo()
    study = tmp_path / "curated" / "MRD-201"
    leftovers = list(study.iterdir())
    assert len(leftovers) == 1
    assert leftovers[0].name.startswith(f".{CUT}.tmp-")
    assert not (study / CUT).exists()
    # A retry still produces a complete cut under the final name.
    result = _ingest(tmp_path, ledger, seed=1)
    assert result.cut_created
    assert (study / CUT / "manifest.json").exists()


def test_same_seed_into_two_folders_gives_identical_hashes(
    tmp_path: Path, ledger: SqliteLedger
) -> None:
    a = _ingest(tmp_path / "a", ledger).manifest
    with make_ledger(tmp_path / "other.sqlite3", FixedClock(T0)) as other:
        b = _ingest(tmp_path / "b", other).manifest
    assert a.file_hashes() == b.file_hashes()
    assert len(a.file_hashes()) == 13
    for path in a.file_hashes():
        assert (tmp_path / "a" / "curated" / "MRD-201" / CUT / path).read_bytes() == (
            tmp_path / "b" / "curated" / "MRD-201" / CUT / path
        ).read_bytes()


def test_different_seed_changes_only_ops(tmp_path: Path, ledger: SqliteLedger) -> None:
    a = _ingest(tmp_path / "a", ledger, seed=1).manifest.file_hashes()
    with make_ledger(tmp_path / "other.sqlite3", FixedClock(T0)) as other:
        b = _ingest(tmp_path / "b", other, seed=2).manifest.file_hashes()
    changed = {p for p in a if a[p] != b[p]}
    assert changed
    assert all(p.startswith("ops/") for p in changed)


def test_data_cut_event_is_appended_once(tmp_path: Path, ledger: SqliteLedger) -> None:
    first = _ingest(tmp_path, ledger)
    second = _ingest(tmp_path, ledger)
    assert first.cut_created
    assert first.event_appended
    assert not second.cut_created
    assert not second.event_appended
    cuts = ledger.read(event_types=[EventType.DATA_CUT])
    assert len(cuts) == 1
    assert first.event is not None
    assert second.event is not None
    assert first.event.seq == second.event.seq == cuts[0].seq
    payload = first.event.payload
    assert payload.type is EventType.DATA_CUT
    assert first.event.actor == "system.ingest"
    assert first.event.visibility.value == "blinded"
    assert verify(ledger).ok


def test_rewriting_an_existing_cut_with_other_content_fails(
    tmp_path: Path, ledger: SqliteLedger
) -> None:
    _ingest(tmp_path, ledger, seed=1)
    before = {p: sha256_file(p) for p in (tmp_path / "curated").rglob("*.parquet")}
    with pytest.raises(CutExistsError):
        _ingest(tmp_path, ledger, seed=2)
    after = {p: sha256_file(p) for p in (tmp_path / "curated").rglob("*.parquet")}
    assert before == after
    assert len(ledger.read(event_types=[EventType.DATA_CUT])) == 1

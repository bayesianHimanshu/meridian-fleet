"""The quality zone's inputs: the cut's blinded tables, plus the blinded area of its eTMF
snapshot staged as tmf/; nothing else, and no other zone gets tmf/."""

from pathlib import Path

import pytest

from meridian.data.study_store import (
    LayeredStudyReader,
    ParquetStudyReader,
    StudyAccessError,
    TmfStageReader,
)
from meridian.domain import tmf as t
from meridian.platform.clock import FixedClock
from meridian.platform.tmf_reference import load_tmf_reference
from meridian.sandbox import policies
from meridian.sandbox.staging import (
    StagingError,
    assert_clean_stage,
    cut_areas_for,
    stage_cut,
    uploads,
)
from meridian.tmf.generate import ensure_clean_snapshot
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY
from tests.tmf_support import tmf_base


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    cut = tmf_base(tmp_path)
    ensure_clean_snapshot(
        etmf_root=tmp_path / "etmf",
        study_id=STUDY,
        cut=cut,
        ref=load_tmf_reference(),
        clock=FixedClock(NOW),
    )
    return tmp_path


def _stage(roots: Path, name: str = "st") -> Path:
    areas = cut_areas_for(policies.ALL_POLICIES["quality"].reads)
    assert areas == ("blinded",)  # no operational data for the quality zone
    return stage_cut(
        roots / "curated" / STUDY / "DC-BASE",
        trigger("DC-BASE"),
        roots / name,
        (),
        areas,
        roots / "etmf" / STUDY / "DC-BASE" / "blinded",
    )


def test_the_stage_holds_the_etmf_tables_under_tmf(roots: Path) -> None:
    stage = _stage(roots)
    assert_clean_stage(stage, ("blinded", "tmf"))
    assert sorted(p.name for p in (stage / "tmf").iterdir()) == sorted(
        f"{name}.parquet" for name in t.TABLES
    )
    assert not (stage / "ops").exists()
    # Each tmf file lands in /stage/tmf, which only the quality policy lists.
    assert {dest for _, dest in uploads(stage)} == {"/stage/meta", "/stage/blinded", "/stage/tmf"}
    with pytest.raises(StagingError):
        assert_clean_stage(stage, ("blinded",))  # tmf/ is not allowed unless asked for


def test_only_a_blinded_etmf_area_may_be_staged(roots: Path) -> None:
    with pytest.raises(StagingError, match="only a blinded area"):
        stage_cut(
            roots / "curated" / STUDY / "DC-BASE",
            trigger("DC-BASE"),
            roots / "st2",
            (),
            ("blinded",),
            roots / "etmf" / STUDY / "DC-BASE" / "unblinded",
        )


def test_the_sandbox_reader_layers_the_staged_tmf_tables(roots: Path) -> None:
    stage = _stage(roots)
    ids = {"cut_id": "DC-BASE", "study_id": STUDY}
    reader = LayeredStudyReader(
        ParquetStudyReader.at_cut_dir(stage, **ids), TmfStageReader.at_cut_dir(stage, **ids)
    )
    assert set(t.TABLES) <= set(reader.tables())
    assert "dm" in reader.tables()
    assert reader.table(t.ETMF_INDEX).height > 0
    with pytest.raises(StudyAccessError):
        TmfStageReader.at_cut_dir(stage, cut_id="../x", study_id=STUDY)

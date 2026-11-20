"""A safety agent's inputs: the cut's blinded and ops tables plus the blinded case table
of its safety database snapshot, staged and read the same way; nothing unblinded."""

from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.safety.sae_reconciler import AGENT_ID
from meridian.data.study_store import (
    LayeredStudyReader,
    ParquetStudyReader,
    StudyAccessError,
)
from meridian.domain import safety as s
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.safetydb.generate import ensure_clean_snapshot
from meridian.sandbox.executor import InProcessExecutor
from meridian.sandbox.staging import StagingError, assert_clean_stage, stage_cut
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY
from tests.safety_support import base_cut


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    cut = base_cut(tmp_path)
    ensure_clean_snapshot(
        safety_root=tmp_path / "safety", study_id=STUDY, cut=cut, clock=FixedClock(NOW)
    )
    return tmp_path


def test_the_stage_holds_the_case_table_but_no_unblinded_file(roots: Path) -> None:
    snap = roots / "safety" / STUDY / "DC-BASE"
    stage = stage_cut(
        roots / "curated" / STUDY / "DC-BASE",
        trigger("DC-BASE"),
        roots / "st",
        [snap / "blinded"],
    )
    assert_clean_stage(stage)
    assert (stage / "blinded" / f"{s.SAFETY_CASES}.parquet").is_file()
    assert not list(stage.rglob(f"{s.SAFETY_UNBLINDED}*"))
    assert s.TREATMENT not in (stage / "blinded" / f"{s.SAFETY_CASES}.parquet").read_bytes().decode(
        "latin-1"
    )
    with pytest.raises(StagingError, match="only a blinded area"):
        stage_cut(
            roots / "curated" / STUDY / "DC-BASE",
            trigger("DC-BASE"),
            roots / "st2",
            [snap / "unblinded"],
        )


def test_the_layered_reader_adds_only_the_blinded_case_table(roots: Path) -> None:
    clinical = ParquetStudyReader(roots / "curated" / STUDY, "DC-BASE")
    safety = ParquetStudyReader(roots / "safety" / STUDY, "DC-BASE")
    layered = LayeredStudyReader(clinical, safety)
    assert s.SAFETY_CASES in layered.tables()
    assert s.SAFETY_UNBLINDED not in layered.tables()
    assert layered.table("ae").height > 0
    with pytest.raises(StudyAccessError):
        LayeredStudyReader(clinical, clinical)  # a table name from both stores


def test_the_in_process_executor_gives_only_safety_agents_the_case_table(roots: Path) -> None:
    agents = Settings().agents
    registry = build_registry(agents)
    executor = InProcessExecutor(
        registry=registry,
        reader_factory=lambda cut: ParquetStudyReader(roots / "curated" / STUDY, cut),
        safety_reader_factory=lambda cut: ParquetStudyReader(roots / "safety" / STUDY, cut),
        log=structlog.get_logger(),
    )
    result = executor.run(AGENT_ID, trigger("DC-BASE"), "DC-BASE")
    assert result.drafts == ()  # a clean snapshot reconciles to zero
    rbqm = set(executor._reader("rbqm.csm", "DC-BASE").tables())
    assert rbqm == set(ParquetStudyReader(roots / "curated" / STUDY, "DC-BASE").tables())
    assert s.SAFETY_CASES not in rbqm

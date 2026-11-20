"""Run preclinical.tox_study_monitor in process on an evaluation copy (tables in memory),
through the agent's own run(); nothing touches the ledger. Kept apart from meridian.evals,
which scores findings and never imports agent code."""

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import polars as pl
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.preclinical.tox_study_monitor import ToxStudyMonitor
from meridian.data.study_store import MissingTableError
from meridian.domain.enums import Visibility
from meridian.domain.events import Event, SendStudyLoaded
from meridian.platform.agent_settings import ToxStudyMonitorConfig
from meridian.platform.clock import FixedClock


class MemoryReader:
    """A StudyReader over tables in memory (an evaluation copy)."""

    def __init__(self, tables: Mapping[str, pl.DataFrame], study_id: str, cut_id: str) -> None:
        self._tables = dict(tables)
        self._study_id = study_id
        self._cut_id = cut_id

    @property
    def cut_id(self) -> str:
        return self._cut_id

    @property
    def study_id(self) -> str:
        return self._study_id

    def table(self, name: str) -> pl.DataFrame:
        if name not in self._tables:
            raise MissingTableError(name)
        return self._tables[name]

    def tables(self) -> list[str]:
        return sorted(self._tables)


EVAL_TIME = datetime(2026, 11, 15, tzinfo=UTC)


def _trigger(study_id: str, cut_id: str) -> Event:
    return Event(
        seq=1,
        event_id=uuid.UUID("01890000-0000-7000-8000-000000000001"),
        occurred_at=EVAL_TIME,
        recorded_at=EVAL_TIME,
        actor="evaluation",
        study_id=study_id,
        visibility=Visibility.BLINDED,
        payload=SendStudyLoaded(study_id=study_id, cut_id=cut_id, path="memory"),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def run_copy(
    tables: Mapping[str, pl.DataFrame], cfg: ToxStudyMonitorConfig, study_id: str, cut_id: str
) -> list[FindingDraft]:
    reader = MemoryReader(tables, study_id, cut_id)
    ctx = AgentContext(
        study=reader,
        clock=FixedClock(EVAL_TIME),
        config=cfg,
        log=structlog.get_logger().bind(cut_id=cut_id),
    )
    return list(ToxStudyMonitor().run(_trigger(study_id, cut_id), ctx))


def run_checks(
    cfg: ToxStudyMonitorConfig, study_id: str
) -> Callable[[Mapping[str, pl.DataFrame], str], dict[str, str]]:
    """A function (tables, cut id) to the checks of the drafts, each with its summary
    text, for meridian.evals."""

    def run(tables: Mapping[str, pl.DataFrame], cut_id: str) -> dict[str, str]:
        return {
            d.dedup_key.rsplit("|", 1)[1]: d.summary
            for d in run_copy(tables, cfg, study_id, cut_id)
        }

    return run

"""Helpers to run an agent against a cut written from rbqm_support tables."""

import uuid
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import structlog

from meridian.agents.base import Agent, AgentConfig, AgentContext, FindingDraft
from meridian.data.study_store import ParquetStudyReader
from meridian.domain.enums import Visibility
from meridian.domain.events import DataCut, Event
from meridian.faults.catalog import REGISTRY, FaultSpec
from meridian.platform.clock import FixedClock
from tests.rbqm_support import NOW, STUDY, make_tables, write_tables_cut

Tables = dict[str, pl.DataFrame]


def trigger(cut_id: str, seq: int = 1) -> Event:
    return Event(
        seq=seq,
        event_id=uuid.uuid7(),
        occurred_at=NOW,
        recorded_at=NOW,
        actor="system.ingest",
        study_id=STUDY,
        visibility=Visibility.BLINDED,
        payload=DataCut(cut_id=cut_id, study_id=STUDY, path=f"curated/{STUDY}/{cut_id}"),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def plant(
    blinded: Tables, ops: Tables, fault_type: str, target: str, params: dict[str, float]
) -> tuple[Tables, Tables]:
    spec = FaultSpec(fault_id="F-001", fault_type=fault_type, target=target, params=params)
    out, _ = REGISTRY[fault_type].fn({**blinded, **ops}, spec, np.random.default_rng(3))
    return {k: out[k] for k in blinded}, {k: out[k] for k in ops}


def run_agent(
    agent: Agent[Any],
    config: AgentConfig,
    root: Path,
    cut_id: str,
    blinded: Tables,
    ops: Tables,
    unblinded: Tables,
) -> list[FindingDraft]:
    study = write_tables_cut(root, cut_id, blinded, ops, unblinded)
    reader = ParquetStudyReader(study, cut_id)
    ctx = AgentContext(
        study=reader, clock=FixedClock(NOW), config=config, log=structlog.get_logger()
    )
    return list(agent.run(trigger(cut_id), ctx))


def clean(per_site: int = 20, small_site: int | None = None) -> tuple[Tables, Tables, Tables]:
    return make_tables(per_site=per_site, seed=7, small_site=small_site)

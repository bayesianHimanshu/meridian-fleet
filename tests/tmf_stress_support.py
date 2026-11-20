"""The hand-written TMF stress set (tests/fixtures/tmf_stress): each case applied to the
hand-written base as plain dicts, with no generator and no normalisation of the agent's."""

import copy
import json
from pathlib import Path
from typing import Any

import polars as pl
import structlog

from meridian.agents.base import AgentContext
from meridian.agents.quality.tmf_completeness import TmfCompleteness
from meridian.data.study_store import MissingTableError
from meridian.domain.events import DataCut, Event
from meridian.platform.agent_settings import TmfCompletenessConfig
from meridian.platform.clock import FixedClock
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY

ROOT = Path(__file__).resolve().parent / "fixtures" / "tmf_stress"
CUT = "DC-TMFSTRESS"


def cases() -> list[Path]:
    return sorted(p for p in ROOT.glob("[0-9][0-9]_*.json"))


def load(path: Path) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(path.read_text())
    return spec


def tables(spec: dict[str, Any]) -> dict[str, pl.DataFrame]:
    base = load(ROOT / "_base.json")
    dm = copy.deepcopy(base["dm"])
    for subject, fields in spec.get("dm_set", {}).items():
        next(r for r in dm if r["USUBJID"] == subject).update(fields)
    gone_visits = set(spec.get("visits_remove", []))
    visits = [v for v in base["visits"] if v["VISIT_ID"] not in gone_visits]
    visits += spec.get("visits_add", [])
    gone = set(spec.get("index_remove", []))
    index = [dict(r) for r in base["index"] if r["DOCUMENT_ID"] not in gone]
    for doc, fields in spec.get("index_set", {}).items():
        next(r for r in index if r["DOCUMENT_ID"] == doc).update(fields)
    index += spec.get("index_add", [])
    export = spec.get("export_date", base["export_date"])
    return {
        "dm": pl.DataFrame(dm),
        "etmf_index": pl.DataFrame(index, schema=dict.fromkeys(index[0], pl.String)),
        "monitoring_visits": pl.DataFrame(visits, schema=dict.fromkeys(visits[0], pl.String)),
        "etmf_export": pl.DataFrame({"EXPORT_DATE": [export]}),
    }


class Reader:
    study_id = STUDY
    cut_id = CUT

    def __init__(self, t: dict[str, pl.DataFrame]) -> None:
        self._t = t

    def table(self, name: str) -> pl.DataFrame:
        if name not in self._t:
            raise MissingTableError(name)
        return self._t[name]

    def tables(self) -> list[str]:
        return sorted(self._t)


def outcome(spec: dict[str, Any]) -> list[str]:
    """What the agent reports: sorted check:record, or "run fails: <error>"."""
    ctx = AgentContext(
        study=Reader(tables(spec)),
        clock=FixedClock(NOW),
        config=TmfCompletenessConfig(),
        log=structlog.get_logger(),
    )
    event: Event = trigger(CUT)
    assert isinstance(event.payload, DataCut)
    try:
        drafts = TmfCompleteness().run(event, ctx)
    except Exception as exc:  # the stress set records a crash as an outcome
        return [f"run fails: {type(exc).__name__}"]
    return sorted(d.dedup_key.split("|")[5] for d in drafts)

"""Hand-written SAE reconciliation tables (the stress sets and the 1.1 unit tests), built
straight from dicts: no generator, no normalisation of ours."""

import json
from pathlib import Path
from typing import Any

import polars as pl
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.safety.sae_reconciler import SaeReconciler
from meridian.domain.events import DataCut, Event
from meridian.platform.agent_settings import SaeReconcilerConfig
from meridian.platform.clock import FixedClock
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY

ROOT = Path(__file__).resolve().parent / "fixtures"
DEV = ROOT / "sae_stress"
HOLDOUT = ROOT / "sae_stress_holdout"
AE_CRITERIA = ("AESDTH", "AESLIFE", "AESHOSP", "AESDISAB", "AESCONG")
CASE_CRITERIA = ("SDTH", "SLIFE", "SHOSP", "SDISAB", "SCONG")
CUT = "DC-STRESS"


def load(path: Path) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(path.read_text())
    return spec


def _frame(rows: list[dict[str, Any]], defaults: dict[str, Any]) -> pl.DataFrame:
    filled = [{**defaults, **r} for r in rows]
    columns = list(dict.fromkeys(k for r in filled for k in r))
    return pl.DataFrame([{c: r.get(c) for c in columns} for r in filled])


def tables(spec: dict[str, Any]) -> dict[str, pl.DataFrame]:
    ae = _frame(spec["ae"], {"STUDYID": STUDY, "DOMAIN": "AE", **dict.fromkeys(AE_CRITERIA, "N")})
    cases = _frame(spec["safety_cases"], dict.fromkeys(CASE_CRITERIA, "N"))
    return {"dm": pl.DataFrame(spec["dm"]), "ae": ae, "safety_cases": cases}


class Reader:
    study_id = STUDY
    cut_id = CUT

    def __init__(self, t: dict[str, pl.DataFrame]) -> None:
        self._t = t

    def table(self, name: str) -> pl.DataFrame:
        return self._t[name]

    def tables(self) -> list[str]:
        return sorted(self._t)


def run_tables(
    t: dict[str, pl.DataFrame], cfg: SaeReconcilerConfig | None = None
) -> list[FindingDraft]:
    ctx = AgentContext(
        study=Reader(t),
        clock=FixedClock(NOW),
        config=cfg or SaeReconcilerConfig(),
        log=structlog.get_logger(),
    )
    event: Event = trigger(CUT)
    assert isinstance(event.payload, DataCut)
    return list(SaeReconciler().run(event, ctx))


def run(spec: dict[str, Any], cfg: SaeReconcilerConfig | None = None) -> list[FindingDraft]:
    return run_tables(tables(spec), cfg)


def check_of(d: FindingDraft) -> str:
    return d.dedup_key.split("|")[5]


def outcome(spec: dict[str, Any]) -> list[str]:
    """What the agent reports: sorted check:record of each draft, or "run fails: <error>"."""
    try:
        drafts = run(spec)
    except Exception as exc:  # the stress set records a crash as an outcome
        return [f"run fails: {type(exc).__name__}"]
    return sorted(check_of(d) for d in drafts)

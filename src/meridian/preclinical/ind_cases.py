"""Inventories for testing preclinical.ind_readiness (phase 5 step 2): the stress cases'
bases and changes (tests/fixtures/ind_stress, docs/ind-readiness-eval.md), and seeded
faults for regression. Nothing here is read by the agent itself."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from meridian.platform.agent_settings import IndReadinessConfig
from meridian.preclinical.inventory import COLUMNS, InventoryStudy, generate_inventory
from meridian.preclinical.readiness import (
    DURATION_SHORT,
    GLP_MISSING,
    MISSING,
    REPORT_NOT_READY,
    assess,
    percent_met,
)

Row = dict[str, str]


def real_rows() -> list[Row]:
    return [s.row() for s in generate_inventory()]


def apply_changes(rows: Sequence[Row], changes: Sequence[Mapping[str, Any]]) -> list[Row]:
    out = [dict(r) for r in rows]
    for change in changes:
        if "drop" in change:
            ids = [r["study_id"] for r in out]
            if change["drop"] not in ids:
                raise KeyError(f"no study {change['drop']} to drop")
            out = [r for r in out if r["study_id"] != change["drop"]]
        elif "set" in change:
            (row,) = [r for r in out if r["study_id"] == change["set"]]
            for k, v in change["fields"].items():
                if k not in COLUMNS:
                    raise KeyError(f"no column {k}")
                row[k] = str(v)
        elif "add" in change:
            out.append({k: str(change["add"][k]) for k in COLUMNS})
        else:
            raise ValueError(f"unknown change {change}")
    return out


def complete_rows(stress_dir: Path) -> list[Row]:
    base = json.loads((stress_dir / "_complete.json").read_text())
    return apply_changes(real_rows(), base["changes"])


def to_studies(rows: Sequence[Row]) -> list[InventoryStudy]:
    import polars as pl

    from meridian.preclinical.readiness import studies_from

    frame = pl.DataFrame(
        {c: [r[c] for r in rows] for c in COLUMNS}, schema=dict.fromkeys(COLUMNS, pl.String)
    )
    return studies_from(frame)


@dataclass(frozen=True, slots=True)
class CaseResult:
    findings: list[str]
    percent_met: float


def case_rows(case: Mapping[str, Any], stress_dir: Path) -> list[Row]:
    base: list[Row]
    if case["base"] == "real":
        base = real_rows()
    elif case["base"] == "complete":
        base = complete_rows(stress_dir)
    elif case["base"] == "empty":
        base = []
    else:
        raise ValueError(f"unknown base {case['base']!r}")
    return apply_changes(base, case["changes"])


def case_config(case: Mapping[str, Any], cfg: IndReadinessConfig) -> IndReadinessConfig:
    trial = dict(case.get("trial", {}))
    if "planned_submission_date" in trial:
        trial["planned_submission_date"] = date.fromisoformat(trial["planned_submission_date"])
    return cfg.model_copy(update=trial)


def evaluate(rows: Sequence[Row], cfg: IndReadinessConfig) -> CaseResult:
    statuses = assess(to_studies(rows), cfg)
    findings = sorted(c for s in statuses for c in s.checks())
    return CaseResult(findings, round(percent_met(statuses), 1))


@dataclass(frozen=True, slots=True)
class Fault:
    seed: int
    kind: str
    rows: list[Row]
    expected: str  # the one check the fault must raise


FAULT_KINDS = ("drop", "shorten", "delay_report", "blank_glp")


def make_fault(seed: int, complete: Sequence[Row], cfg: IndReadinessConfig) -> Fault:
    """One seeded fault on the complete inventory, with the check it must raise: a pivotal
    study dropped (its requirement then has no candidate), a repeat-dose study shortened
    below the required duration, a QA audit moved past the submission date, or a GLP
    status blanked. Only studies that are the sole candidate of their requirement are
    touched, so exactly one gap appears."""
    rng = np.random.default_rng(seed)
    kind = FAULT_KINDS[int(rng.integers(0, len(FAULT_KINDS)))]
    statuses = assess(to_studies(complete), cfg)
    sole = [
        s
        for s in statuses
        if s.applicable and len(s.candidates) == 1 and s.study is not None and s.met
    ]
    if kind == "shorten":
        sole = [s for s in sole if s.required_weeks is not None]
        if not sole:  # every repeat-dose requirement has two candidates: shorten both
            sole = [s for s in statuses if s.required_weeks is not None and s.met]
    st = sole[int(rng.integers(0, len(sole)))]
    assert st.study is not None
    sid, rid = st.study.study_id, st.requirement.id
    if kind == "drop":
        changes: list[dict[str, Any]] = [{"drop": sid}]
        expected = f"{MISSING}:{rid}"
    elif kind == "shorten":
        need = st.required_weeks or 2.0
        changes = [
            {"set": c.study_id, "fields": {"duration_weeks": f"{need / 2:g}"}}
            for c in st.candidates
        ]
        expected = f"{DURATION_SHORT}:{rid}:{sid}"
    elif kind == "delay_report":
        late = date.fromordinal(cfg.planned_submission_date.toordinal() + 30)
        changes = [{"set": sid, "fields": {"report_status": "draft", "qa_audit_date": str(late)}}]
        expected = f"{REPORT_NOT_READY}:{rid}:{sid}"
    else:
        changes = [{"set": sid, "fields": {"glp": ""}}]
        expected = f"{GLP_MISSING}:{rid}:{sid}"
    return Fault(seed, kind, apply_changes(complete, changes), expected)

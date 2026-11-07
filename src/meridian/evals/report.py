"""docs/eval-phase1.md: the phase 1 exit report, built from scorecards and blinded data."""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.data.study_store import StudyReader
from meridian.evals.scorecard import CutEvaluation, ScorecardStore
from meridian.faults.inject import GroundTruth
from meridian.faults.scenario import SweepItem, sweep_items
from meridian.platform.agent_settings import CsmConfig, KriMonitorConfig, QtlDetectorConfig
from meridian.stats.robust import quasi_poisson
from meridian.stats.site_metrics import (
    counts_by_site,
    exposure,
    sites_with_min_treated,
    vital_stats,
)

FREEZE_MESSAGE = "Freeze phase 1 thresholds before holdout"
BASE_CUT = "DC-BASE"
DEV_CUT = "DC-RBQM-CORE"
HOLDOUT_CUT = "DC-RBQM-HOLDOUT"
NULL_CUTS: tuple[tuple[str, int], ...] = tuple((f"DC-NULL-S{i}", 100 + i) for i in range(1, 6))
V1_LABEL = "all agents at 1.0.0"


def frozen_commit(cwd: Path | None = None) -> str:
    """The commit whose message is the freeze message (read-only git log)."""
    try:
        done = subprocess.run(  # noqa: S603 (fixed argv, read-only git log)
            ["git", "log", "-1", "--format=%H", f"--grep={FREEZE_MESSAGE}"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return done.stdout.strip() or "not frozen yet"


@dataclass(frozen=True, slots=True)
class ReportInputs:
    store: ScorecardStore
    v2_label: str
    agents: Sequence[str]
    base_study: StudyReader
    ground_truth_dir: Path
    kri: KriMonitorConfig
    qtl: QtlDetectorConfig
    csm: CsmConfig
    ledger_ok: bool
    ledger_events: int
    frozen_sha: str
    head_sha: str


def _f(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _need(store: ScorecardStore, cut: str, label: str) -> CutEvaluation | None:
    return store.latest(cut, label)


def _fp_by_agent(e: CutEvaluation | None, agent: str) -> str:
    if e is None:
        return "not run"
    m = e.metrics.per_agent.get(agent)
    return str(m.false_positives if m else 0)


def _recall_by_agent(e: CutEvaluation | None, agent: str) -> str:
    if e is None:
        return "not run"
    m = e.metrics.per_agent.get(agent)
    return _f(m.recall if m else None)


def _site_table(inp: ReportInputs) -> list[str]:
    study = inp.base_study
    dm, sv = study.table("dm"), study.table("sv")
    exp = exposure(dm, sv)
    included = exp.per_subject.select("USUBJID", "SITEID")
    tested, _ = sites_with_min_treated(dm, inp.kri.min_subjects)
    ae = quasi_poisson(
        counts_by_site(study.table("ae"), included), exp.days(), tested, lower_tail=True
    )
    dev = quasi_poisson(
        counts_by_site(study.table("deviations"), included), exp.days(), tested, lower_tail=False
    )
    csm_tested, _ = sites_with_min_treated(dm, inp.csm.min_subjects)
    vitals = vital_stats(
        dm, study.table("vs"), csm_tested, inp.csm.parameters, inp.csm.min_readings
    )
    ae_z = {s.site: s.z for s in ae.sites}
    dev_z = {s.site: s.z for s in dev.sites}
    params = [v.parameter for v in vitals]
    head = (
        "| Site | Treated | AE z | Deviations z | "
        + " | ".join(f"{p} log-SD z" for p in params)
        + " | "
        + " | ".join(f"{p} 0-share (z)" for p in params)
        + " |"
    )
    lines = [
        f"AE dispersion phi = {ae.phi:.2f}; deviations phi = {dev.phi:.2f}. Sites with fewer "
        f"than {inp.kri.min_subjects} treated subjects are not tested (n/a).",
        "",
        head,
        "| " + " | ".join(["---"] + ["---:"] * (3 + 2 * len(params))) + " |",
    ]
    for site, treated in exp.frame.select("SITEID", "treated").iter_rows():
        sd_cells, zero_cells = [], []
        for v in vitals:
            stat = v.sites.get(site)
            sd_cells.append(_f(stat.sd_z if stat else None))
            if stat is None or stat.zero_share is None:
                zero_cells.append("n/a")
            else:
                zero_cells.append(f"{stat.zero_share:.2f} ({_f(stat.zero_z)})")
        lines.append(
            f"| {site} | {treated} | {_f(ae_z.get(site))} | {_f(dev_z.get(site))} | "
            + " | ".join(sd_cells)
            + " | "
            + " | ".join(zero_cells)
            + " |"
        )
    return lines


def _sweep_rows(inp: ReportInputs) -> tuple[list[str], list[str]]:
    rows = [
        "| Fault type | Level | Cut | Target | Detected | Finding | Score |",
        "| --- | --- | --- | --- | --- | --- | ---: |",
    ]
    smallest: dict[str, SweepItem | None] = {}
    ran: dict[str, bool] = {}
    for item in sweep_items():
        e = _need(inp.store, item.out_cut, inp.v2_label)
        truth_path = inp.ground_truth_dir / f"{item.out_cut}.json"
        target = GroundTruth.load(truth_path).faults[0].target if truth_path.exists() else "?"
        level = (
            f"base + {item.level:.2f}"
            if item.fault_type == "discontinuation_drift"
            else f"{item.parameter} {item.level:g}"
        )
        if e is None:
            rows.append(
                f"| {item.fault_type} | {level} | {item.out_cut} | {target} | not run | | |"
            )
            continue
        ran[item.fault_type] = True
        (match,) = e.matches
        rows.append(
            f"| {item.fault_type} | {level} | {item.out_cut} | {target} | "
            f"{'yes' if match.detected else 'no'} | {match.finding_id or ''} | "
            f"{_f(match.score)} |"
        )
        # Levels are listed from the weakest effect to the strongest.
        if match.detected and smallest.get(item.fault_type) is None:
            smallest[item.fault_type] = item
        smallest.setdefault(item.fault_type, None)
    lines: list[str] = []
    for fault_type in dict.fromkeys(i.fault_type for i in sweep_items()):
        found = smallest.get(fault_type)
        if not ran.get(fault_type):
            lines.append(f"* {fault_type}: not run.")
        elif found is None:
            lines.append(f"* {fault_type}: not detected at any level.")
        elif found.fault_type == "discontinuation_drift":
            lines.append(f"* {fault_type}: smallest effect detected is base + {found.level:.2f}.")
        else:
            lines.append(
                f"* {fault_type}: smallest effect detected is {found.parameter} {found.level:g}."
            )
    return rows, lines


def render(inp: ReportInputs) -> str:
    store = inp.store
    core_v2 = _need(store, DEV_CUT, inp.v2_label)
    hold_v2 = _need(store, HOLDOUT_CUT, inp.v2_label)
    base_v2 = _need(store, BASE_CUT, inp.v2_label)
    core_v1 = _need(store, DEV_CUT, V1_LABEL)
    base_v1 = _need(store, BASE_CUT, V1_LABEL)
    nulls = [(cut, _need(store, cut, inp.v2_label)) for cut, _ in NULL_CUTS]
    null_fps = [e.metrics.overall.false_positives for _, e in nulls if e is not None]
    mean_null = sum(null_fps) / len(null_fps) if null_fps else None

    def recall(e: CutEvaluation | None) -> float | None:
        return None if e is None else e.metrics.overall.recall

    def verdict(ok: bool | None) -> str:
        return "not run" if ok is None else ("pass" if ok else "FAIL")

    core_r, hold_r = recall(core_v2), recall(hold_v2)
    base_fp = None if base_v2 is None else base_v2.metrics.overall.false_positives
    criteria = [
        ("Recall on DC-RBQM-CORE >= 0.8", _f(core_r), None if core_r is None else core_r >= 0.8),
        ("Recall on DC-RBQM-HOLDOUT >= 0.8", _f(hold_r), None if hold_r is None else hold_r >= 0.8),
        (
            "False positives on DC-BASE <= 1",
            str(base_fp) if base_fp is not None else "n/a",
            None if base_fp is None else base_fp <= 1,
        ),
        (
            "Mean false positives on DC-NULL-S1 to S5 <= 1 per cut",
            _f(mean_null),
            None if mean_null is None or len(null_fps) < len(NULL_CUTS) else mean_null <= 1,
        ),
        ("Ledger verifies", f"{inp.ledger_events} events", inp.ledger_ok),
    ]
    lines = [
        "# Phase 1 evaluation",
        "",
        "Generated by `meridian eval report` from the scorecards in data/evals, the ground "
        "truth files and the blinded DC-BASE cut. "
        f"Agents scored: {inp.v2_label}. Report built at commit `{inp.head_sha}`.",
        "",
        "## 1. Exit criteria",
        "",
        "| Criterion | Value | Result |",
        "| --- | --- | --- |",
        *(f"| {name} | {value} | {verdict(ok)} |" for name, value, ok in criteria),
        "",
        "## 2. v1 versus v2",
        "",
        "False positives on DC-BASE (clean) and recall on DC-RBQM-CORE, per agent. v1 is the "
        "untuned step 3 baseline, still in the ledger.",
        "",
        "| Agent | DC-BASE FP v1 | DC-BASE FP v2 | DC-RBQM-CORE recall v1 "
        "| DC-RBQM-CORE recall v2 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for agent in sorted(inp.agents):
        lines.append(
            f"| {agent} | {_fp_by_agent(base_v1, agent)} | {_fp_by_agent(base_v2, agent)} | "
            f"{_recall_by_agent(core_v1, agent)} | {_recall_by_agent(core_v2, agent)} |"
        )
    total = [
        str(e.metrics.overall.false_positives) if e else "not run" for e in (base_v1, base_v2)
    ] + [_f(recall(e)) if e else "not run" for e in (core_v1, core_v2)]
    lines += [f"| all | {' | '.join(total)} |", ""]
    if core_v2 is not None:
        lines += ["DC-RBQM-CORE with v2:", "", *_match_table(core_v2), ""]

    lines += ["## 3. Holdout (DC-RBQM-HOLDOUT)", ""]
    if hold_v2 is None:
        lines += ["Not run yet.", ""]
    else:
        m = hold_v2.metrics.overall
        lines += [
            f"Recall {_f(m.recall)}, false positives {m.false_positives}, average precision "
            f"{_f(m.average_precision)}. Thresholds were frozen before this cut existed.",
            "",
            *_match_table(hold_v2),
            "",
        ]

    lines += [
        "## 4. Null cuts",
        "",
        "Same AE and VS data as DC-BASE, operational tables drawn with ops seeds 101 to 105.",
        "Every finding is a false positive.",
        "",
        "| Cut | Ops seed | " + " | ".join(sorted(inp.agents)) + " | Total |",
        "| --- | ---: | " + " | ".join("---:" for _ in inp.agents) + " | ---: |",
    ]
    for (cut, seed), (_, e) in zip(NULL_CUTS, nulls, strict=True):
        cells = [_fp_by_agent(e, a) for a in sorted(inp.agents)]
        total_fp = "not run" if e is None else str(e.metrics.overall.false_positives)
        lines.append(f"| {cut} | {seed} | {' | '.join(cells)} | {total_fp} |")
    lines += ["", f"Mean false positives per null cut: {_f(mean_null)}.", ""]

    sweep_table, smallest = _sweep_rows(inp)
    lines += [
        "## 5. Magnitude sweep",
        "",
        "One fault per cut, at the site it had in rbqm_core. Levels run from the weakest "
        "effect to the strongest; some are designed to be missed.",
        "",
        *sweep_table,
        "",
        *smallest,
        "",
        "## 6. Sites on DC-BASE (v2 statistics)",
        "",
        *_site_table(inp),
        "",
        "## 7. Frozen thresholds",
        "",
        f'Frozen in commit `{inp.frozen_sha}` ("{FREEZE_MESSAGE}").',
        "",
        "| Agent | Threshold | Value |",
        "| --- | --- | --- |",
        *(f"| rbqm.kri_monitor | {k} | {v} |" for k, v in inp.kri.model_dump().items()),
        *(f"| rbqm.qtl_detector | {k} | {v} |" for k, v in inp.qtl.model_dump().items()),
        *(
            f"| rbqm.csm | {k} | {', '.join(v) if isinstance(v, tuple) else v} |"
            for k, v in inp.csm.model_dump().items()
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _match_table(e: CutEvaluation) -> list[str]:
    rows = [
        "| Fault | Type | Target | Expected | Detected | Finding | Agent | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for m in e.matches:
        expected = m.expected_kind.value + (f" ({m.kri})" if m.kri else "")
        rows.append(
            f"| {m.fault_id} | {m.fault_type} | {m.target} | {expected} | "
            f"{'yes' if m.detected else 'no'} | {m.finding_id or ''} | {m.agent_id or ''} | "
            f"{_f(m.score)} |"
        )
    fps = [f for f in e.findings if f.finding_id in set(e.false_positive_ids)]
    if fps:
        rows += [
            "",
            "False positives: "
            + ", ".join(
                f"{f.finding_id} ({f.agent_id}, {f.kind.value}, {f.site}, {f.check})" for f in fps
            )
            + ".",
        ]
    return rows


def write(path: Path, inp: ReportInputs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(inp), encoding="utf-8")
    return path


__all__ = ["NULL_CUTS", "ReportInputs", "frozen_commit", "render", "write"]

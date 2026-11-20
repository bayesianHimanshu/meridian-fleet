"""docs/eval-phase1.md: the phase 1 exit report, built from scorecards and blinded data."""

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from meridian.data.study_store import StudyReader
from meridian.evals.matching import VersionFilter
from meridian.evals.rules import RuleEvaluation, is_rule_fault
from meridian.evals.scorecard import CutEvaluation, ScorecardStore
from meridian.faults.catalog import FaultRecord
from meridian.faults.inject import GroundTruth
from meridian.faults.scenario import SweepItem, sweep_items
from meridian.platform.agent_settings import CsmConfig, KriMonitorConfig, QtlDetectorConfig
from meridian.stats.ae_expectation import AE_VARIANTS, ae_expectation
from meridian.stats.robust import quasi_poisson, quasi_poisson_test
from meridian.stats.site_metrics import (
    counts_by_site,
    exposure,
    sites_with_min_treated,
    vital_stats,
)

FREEZE_MESSAGE = "Freeze phase 1 thresholds before holdout"
AE_FREEZE_MESSAGE = "Freeze AE expectation variant before holdout 2"
BASE_CUT = "DC-BASE"
DEV_CUT = "DC-RBQM-CORE"
HOLDOUT_CUT = "DC-RBQM-HOLDOUT"
NULL_CUTS: tuple[tuple[str, int], ...] = tuple((f"DC-NULL-S{i}", 100 + i) for i in range(1, 6))
AE_SWEEP_CUTS = tuple(f"DC-SWEEP-AE-UNDER-REPORTING-{x}" for x in ("0P2", "0P4", "0P7"))
V1_LABEL = "all agents at 1.0.0"
V2_VERSIONS = {"rbqm.csm": "2.0.0", "rbqm.kri_monitor": "2.0.0", "rbqm.qtl_detector": "2.0.0"}
V2_LABEL = VersionFilter.registered(V2_VERSIONS).label()
REVISION_NOTE = (
    "The criteria were revised after seeing the step 4 results, because step 4 scored "
    "every rule finding against planted faults only, so a rule finding at a site that "
    "really breaches its threshold (a natural breach, such as a synthetic median entry lag "
    "of 7.4 days against the 7-day threshold) counted as a false positive: an error in "
    "the measurement, not in the agent."
)


def commit_with_message(message: str, cwd: Path | None = None) -> str:
    """The oldest commit whose subject line is exactly `message` (read-only git log).

    The subject must match exactly: later commits may quote the message in their body.
    """
    try:
        done = subprocess.run(  # noqa: S603 (fixed argv, read-only git log)
            ["git", "log", "--format=%H %s", f"--grep={message}", "--fixed-strings"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    exact = [
        sha
        for sha, _, subject in (line.partition(" ") for line in done.stdout.splitlines())
        if subject == message
    ]
    return exact[-1] if exact else "not frozen yet"


def frozen_commit(cwd: Path | None = None) -> str:
    return commit_with_message(FREEZE_MESSAGE, cwd)


def ae_frozen_commit(cwd: Path | None = None) -> str:
    return commit_with_message(AE_FREEZE_MESSAGE, cwd)


@dataclass(frozen=True, slots=True)
class ReportInputs:
    store: ScorecardStore
    v2_label: str  # the registered versions (v2.1 from step 5 on)
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
    ae_frozen_sha: str = "not frozen yet"
    holdout2: tuple[str, str] | None = None  # (cut id, label)
    ae_holdouts: tuple[tuple[str, str], ...] = ()
    freeze_violations: tuple[str, ...] | None = None
    study: Callable[[str], StudyReader] | None = None
    labels: dict[str, str] = field(default_factory=dict)


def _f(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _verdict(ok: bool | None) -> str:
    return "not run" if ok is None else ("pass" if ok else "FAIL")


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


def _recall(e: CutEvaluation | None) -> float | None:
    return None if e is None else e.metrics.overall.recall


def _det_recall(e: CutEvaluation | None) -> float | None:
    return None if e is None or e.detector_metrics is None else e.detector_metrics.recall


def _det_fp(e: CutEvaluation | None) -> int | None:
    return None if e is None or e.detector_metrics is None else e.detector_metrics.false_positives


def _rules(e: CutEvaluation | None) -> RuleEvaluation | None:
    return None if e is None else e.rules


def _faults(inp: ReportInputs, cut: str) -> list[FaultRecord]:
    path = inp.ground_truth_dir / f"{cut}.json"
    return list(GroundTruth.load(path).faults) if path.exists() else []


class _Cards:
    """Latest evaluation per (cut, version label)."""

    def __init__(self, store: ScorecardStore) -> None:
        self._latest: dict[tuple[str, str], CutEvaluation] = {}
        for card in store.all():
            for e in card.evaluations:
                self._latest[(e.cut_id, card.agent_versions)] = e

    def get(self, cut: str, label: str) -> CutEvaluation | None:
        return self._latest.get((cut, label))

    def cuts(self, label: str) -> list[str]:
        return sorted(c for c, lab in self._latest if lab == label)


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
        f"AE dispersion phi = {ae.phi:.2f} (variant a); deviations phi = {dev.phi:.2f}. Sites "
        f"with fewer than {inp.kri.min_subjects} treated subjects are not tested (n/a).",
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


def _fault_rows(e: CutEvaluation) -> list[str]:
    """Every planted fault, scored the step 5 way."""
    rows = [
        "| Fault | Type | Target | Check type | Outcome | Finding | Score |",
        "| --- | --- | --- | --- | --- | --- | ---: |",
    ]
    rule_by_fault = {o.fault_id: o for o in (e.rules.outcomes if e.rules else ()) if o.fault_id}
    for m in e.matches:
        if m.fault_id in rule_by_fault:
            o = rule_by_fault[m.fault_id]
            outcome = o.outcome.replace("_", " ")
            value = f" ({_f(o.value)} vs {_f(o.threshold)})"
            rows.append(
                f"| {m.fault_id} | {m.fault_type} | {m.target} | rule | {outcome}{value} | "
                f"{o.finding_id or ''} | |"
            )
            continue
        d = next((x for x in e.detector_matches if x.fault_id == m.fault_id), m)
        rows.append(
            f"| {m.fault_id} | {m.fault_type} | {m.target} | detector | "
            f"{'detected' if d.detected else 'missed'} | {d.finding_id or ''} | {_f(d.score)} |"
        )
    fps = [f for f in e.findings if f.finding_id in set(e.detector_false_positive_ids)]
    if fps:
        rows += [
            "",
            "Detector false positives: "
            + ", ".join(f"{f.finding_id} ({f.check} at {f.site}, score {f.score:.2f})" for f in fps)
            + ".",
        ]
    natural = e.rules.of("natural_breach") if e.rules else []
    if natural:
        rows += [
            "",
            "Natural breaches (correct rule findings with no planted fault): "
            + ", ".join(f"{o.check} at {o.site} ({_f(o.value)})" for o in natural)
            + ".",
        ]
    return rows


def _ae_z(inp: ReportInputs, cut: str, target: str) -> tuple[float | None, float | None]:
    """(z at the target, phi) of the AE test with the frozen variant, from the cut itself."""
    if inp.study is None:
        return None, None
    try:
        study = inp.study(cut)
        dm = study.table("dm")
    except FileNotFoundError, KeyError:
        return None, None
    tested, _ = sites_with_min_treated(dm, inp.kri.min_subjects)
    rows = ae_expectation(
        inp.kri.ae_variant,
        dm=dm,
        sv=study.table("sv"),
        ae=study.table("ae"),
        lb=study.table("lb"),
        tested=tested,
        early_window_days=inp.kri.early_window_days,
    )
    result = quasi_poisson_test(rows, lower_tail=True)
    return next((s.z for s in result.sites if s.site == target), None), result.phi


def _sweep(inp: ReportInputs, cards: _Cards) -> tuple[list[str], list[str]]:
    rows = [
        "| Fault type | Level | Cut | Target | Check type | Outcome | Finding | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    smallest: dict[str, SweepItem | None] = {}
    below: dict[str, list[str]] = {}
    ran: set[str] = set()
    for item in sweep_items():
        e = cards.get(item.out_cut, inp.v2_label)
        faults = _faults(inp, item.out_cut)
        target = faults[0].target if faults else "?"
        level = (
            f"base + {item.level:.2f}"
            if item.fault_type == "discontinuation_drift"
            else f"{item.parameter} {item.level:g}"
        )
        if e is None or not e.matches:
            rows.append(
                f"| {item.fault_type} | {level} | {item.out_cut} | {target} | | not run | | |"
            )
            continue
        ran.add(item.fault_type)
        (m,) = e.matches
        rule = is_rule_fault(faults[0]) if faults else False
        if rule and e.rules is not None:
            (o,) = [x for x in e.rules.outcomes if x.fault_id == m.fault_id]
            hit = o.outcome == "planted_breach"
            outcome = f"{o.outcome.replace('_', ' ')} ({_f(o.value)} vs {_f(o.threshold)})"
            finding, score = o.finding_id or "", ""
            if o.outcome == "below_threshold_by_design":
                below.setdefault(item.fault_type, []).append(level)
        else:
            d = next((x for x in e.detector_matches if x.fault_id == m.fault_id), m)
            hit = d.detected
            outcome = "detected" if hit else "missed"
            finding, score = d.finding_id or "", _f(d.score)
        kind = "rule" if rule else "detector"
        rows.append(
            f"| {item.fault_type} | {level} | {item.out_cut} | {target} | {kind} | {outcome} | "
            f"{finding} | {score} |"
        )
        if hit and smallest.get(item.fault_type) is None:
            smallest[item.fault_type] = item
        smallest.setdefault(item.fault_type, None)
    lines: list[str] = []
    for fault_type in dict.fromkeys(i.fault_type for i in sweep_items()):
        found = smallest.get(fault_type)
        note = ""
        if below.get(fault_type):
            note = f" Below the rule's threshold by design: {', '.join(below[fault_type])}."
        if fault_type not in ran:
            lines.append(f"* {fault_type}: not run.")
        elif found is None:
            lines.append(f"* {fault_type}: not detected at any level.{note}")
        elif found.fault_type == "discontinuation_drift":
            lines.append(
                f"* {fault_type}: smallest effect detected is base + {found.level:.2f}.{note}"
            )
        else:
            lines.append(
                f"* {fault_type}: smallest effect detected is {found.parameter} "
                f"{found.level:g}.{note}"
            )
    return rows, lines


def render(inp: ReportInputs) -> str:
    cards = _Cards(inp.store)
    v21 = inp.v2_label
    step4_cuts = [
        BASE_CUT,
        *(c for c, _ in NULL_CUTS),
        DEV_CUT,
        HOLDOUT_CUT,
        *(i.out_cut for i in sweep_items()),
    ]
    new_cuts = [
        *([inp.holdout2[0]] if inp.holdout2 else []),
        *(c for c, _ in inp.ae_holdouts),
    ]
    labels = {**inp.labels}
    if inp.holdout2:
        labels[inp.holdout2[0]] = inp.holdout2[1]
    labels.update(dict(inp.ae_holdouts))

    def name(cut: str) -> str:
        return f"{cut} ({labels[cut]})" if cut in labels else cut

    def nulls(label: str) -> list[CutEvaluation]:
        return [e for c, _ in NULL_CUTS if (e := cards.get(c, label)) is not None]

    def mean(values: Sequence[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def original(label: str, holdout: str | None) -> list[tuple[str, float | None, bool | None]]:
        core, hold, base = cards.get(DEV_CUT, label), None, cards.get(BASE_CUT, label)
        if holdout is not None:
            hold = cards.get(holdout, label)
        ns = nulls(label)
        base_fp = None if base is None else base.metrics.overall.false_positives
        null_mean = mean([e.metrics.overall.false_positives for e in ns])
        full = len(ns) == len(NULL_CUTS)
        cr, hr = _recall(core), _recall(hold)
        return [
            ("core", cr, None if cr is None else cr >= 0.8),
            ("hold", hr, None if hr is None else hr >= 0.8),
            ("base", base_fp, None if base_fp is None else base_fp <= 1),
            ("null", null_mean, None if null_mean is None or not full else null_mean <= 1),
        ]

    def revised(
        label: str, holdout: str | None, cuts: Sequence[str]
    ) -> list[tuple[str, float | None, bool | None]]:
        core, base = cards.get(DEV_CUT, label), cards.get(BASE_CUT, label)
        hold = cards.get(holdout, label) if holdout else None
        ns = nulls(label)
        base_fp = _det_fp(base)
        null_fps = [fp for e in ns if (fp := _det_fp(e)) is not None]
        null_mean = mean(null_fps)
        full = len(null_fps) == len(NULL_CUTS)
        scored = [r for c in cuts if (r := _rules(cards.get(c, label))) is not None]
        perfect = sum(1 for r in scored if r.agreeing == r.units)
        rules_ok = None if not scored else perfect == len(scored) == len(cuts)
        cr, hr = _det_recall(core), _det_recall(hold)
        return [
            ("core", cr, None if cr is None else cr >= 0.8),
            ("hold", hr, None if hr is None else hr >= 0.8),
            ("base", base_fp, None if base_fp is None else base_fp <= 1),
            ("null", null_mean, None if null_mean is None or not full else null_mean <= 1),
            ("rules", (perfect / len(cuts)) if scored else None, rules_ok),
        ]

    def cell(value: float | None, ok: bool | None, digits: int = 2, as_int: bool = False) -> str:
        if value is None:
            return "n/a"
        text = str(int(value)) if as_int else f"{value:.{digits}f}"
        return f"{text} ({_verdict(ok)})"

    holdout2 = inp.holdout2[0] if inp.holdout2 else None
    o_v2 = original(V2_LABEL, HOLDOUT_CUT)
    o_v21 = original(v21, HOLDOUT_CUT)
    o_v21_h2 = original(v21, holdout2)
    r_v2 = revised(V2_LABEL, HOLDOUT_CUT, step4_cuts)
    r_v21 = revised(v21, holdout2, step4_cuts + new_cuts)
    r_v21_h1 = revised(v21, HOLDOUT_CUT, step4_cuts)
    ledger = f"{inp.ledger_events} events ({_verdict(inp.ledger_ok)})"
    lines = [
        "# Phase 1 evaluation",
        "",
        "Generated by `meridian eval report` from the scorecards in data/evals, the ground "
        "truth files and the blinded cuts. "
        f"v1 is {V1_LABEL}; v2 is {V2_LABEL}; v2.1 is {v21}. "
        f"Report built at commit `{inp.head_sha}`.",
        "",
        "## 1. Exit criteria, original and revised",
        "",
        REVISION_NOTE,
        "",
        "Original step 4 criteria (every finding scored against planted faults):",
        "",
        "| Criterion | v2 (step 4) | v2.1 (step 5) |",
        "| --- | --- | --- |",
        f"| Recall on DC-RBQM-CORE >= 0.8 | {cell(o_v2[0][1], o_v2[0][2])} | "
        f"{cell(o_v21[0][1], o_v21[0][2])} |",
        f"| Recall on DC-RBQM-HOLDOUT >= 0.8 | {cell(o_v2[1][1], o_v2[1][2])} | "
        f"{cell(o_v21[1][1], o_v21[1][2])} |",
        f"| Recall on holdout 2 >= 0.8 | n/a | {cell(o_v21_h2[1][1], o_v21_h2[1][2])} |",
        f"| False positives on DC-BASE <= 1 | {cell(o_v2[2][1], o_v2[2][2], as_int=True)} | "
        f"{cell(o_v21[2][1], o_v21[2][2], as_int=True)} |",
        f"| Mean false positives per null cut <= 1 | {cell(o_v2[3][1], o_v2[3][2])} | "
        f"{cell(o_v21[3][1], o_v21[3][2])} |",
        f"| Ledger verifies | | {ledger} |",
        "",
        "Revised criteria (rules against condition truth, detectors against planted faults):",
        "",
        "| Criterion | v2 (step 4 cuts) | v2.1 (all cuts) |",
        "| --- | --- | --- |",
        f"| Detector recall on DC-RBQM-CORE >= 0.8 | {cell(r_v2[0][1], r_v2[0][2])} | "
        f"{cell(r_v21[0][1], r_v21[0][2])} |",
        f"| Detector recall on the holdout >= 0.8 (v2: DC-RBQM-HOLDOUT, v2.1: holdout 2) | "
        f"{cell(r_v2[1][1], r_v2[1][2])} | {cell(r_v21[1][1], r_v21[1][2])} |",
        f"| Detector recall on DC-RBQM-HOLDOUT >= 0.8 (seen in step 4) | "
        f"{cell(r_v2[1][1], r_v2[1][2])} | {cell(r_v21_h1[1][1], r_v21_h1[1][2])} |",
        f"| Detector false positives on DC-BASE <= 1 | "
        f"{cell(r_v2[2][1], r_v2[2][2], as_int=True)} | "
        f"{cell(r_v21[2][1], r_v21[2][2], as_int=True)} |",
        f"| Mean detector false positives per null cut <= 1 | {cell(r_v2[3][1], r_v2[3][2])} | "
        f"{cell(r_v21[3][1], r_v21[3][2])} |",
        f"| Rules agree 100% with condition truth on every cut (share of cuts) | "
        f"{cell(r_v2[4][1], r_v2[4][2])} | {cell(r_v21[4][1], r_v21[4][2])} |",
        f"| Ledger verifies | | {ledger} |",
        "",
    ]

    # 2. Rules
    lines += [
        "## 2. Rules against condition truth (v2.1)",
        "",
        "Condition truth is computed from each cut with the functions the agents use "
        "(`meridian.stats.rules`). Units are every site with a measured value for "
        "data_entry_lag and query_aging, and the study for premature_discontinuation.",
        "",
        "| Cut | Units | Agreeing | Agreement | Natural breaches | Planted breaches "
        "| Below threshold by design | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for cut in step4_cuts + new_cuts:
        r = _rules(cards.get(cut, v21))
        if r is None:
            lines.append(f"| {name(cut)} | | | not run | | | | |")
            continue
        errors = len(r.of("missing_finding")) + len(r.of("unsupported_finding"))
        lines.append(
            f"| {name(cut)} | {r.units} | {r.agreeing} | {r.agreement:.0%} | "
            f"{len(r.of('natural_breach'))} | {len(r.of('planted_breach'))} | "
            f"{len(r.of('below_threshold_by_design'))} | {errors} |"
        )
    lines += ["", "Natural breaches on the clean cuts:", ""]
    for cut in [BASE_CUT, *(c for c, _ in NULL_CUTS)]:
        r = _rules(cards.get(cut, v21))
        found = r.of("natural_breach") if r else []
        text = ", ".join(
            f"{o.check} at {o.site} ({_f(o.value)} vs {_f(o.threshold)})" for o in found
        )
        lines.append(f"* {cut}: {text or ('none' if r else 'not run')}")
    disagreements = [
        (cut, o)
        for cut in step4_cuts + new_cuts
        if (r := _rules(cards.get(cut, v21))) is not None
        for o in (*r.of("missing_finding"), *r.of("unsupported_finding"))
    ]
    lines += [""]
    if disagreements:
        lines += [
            "Disagreements: "
            + ", ".join(f"{cut} {o.check} at {o.site} ({o.outcome})" for cut, o in disagreements)
            + ".",
            "",
        ]

    # 3. v1, v2, v2.1
    lines += [
        "## 3. v1, v2 and v2.1",
        "",
        "Original scoring (every finding against planted faults), per agent. v2.1 changes "
        "only rbqm.kri_monitor; rbqm.csm and rbqm.qtl_detector stay at 2.0.0.",
        "",
        "| Agent | DC-BASE FP v1 | v2 | v2.1 | DC-RBQM-CORE recall v1 | v2 | v2.1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    labels3 = (V1_LABEL, V2_LABEL, v21)
    for agent in sorted(inp.agents):
        fps = " | ".join(_fp_by_agent(cards.get(BASE_CUT, lab), agent) for lab in labels3)
        recs = " | ".join(_recall_by_agent(cards.get(DEV_CUT, lab), agent) for lab in labels3)
        lines.append(f"| {agent} | {fps} | {recs} |")
    total_fp = " | ".join(
        str(e.metrics.overall.false_positives) if (e := cards.get(BASE_CUT, lab)) else "not run"
        for lab in labels3
    )
    total_rec = " | ".join(
        _f(_recall(e)) if (e := cards.get(DEV_CUT, lab)) else "not run" for lab in labels3
    )
    lines += [f"| all | {total_fp} | {total_rec} |", ""]

    # 4. AE experiment
    variant = inp.kri.ae_variant
    lines += [
        "## 4. AE under-reporting experiment",
        "",
        f"Pre-registered in docs/ae-experiment.md; frozen in commit `{inp.ae_frozen_sha}` "
        f'("{AE_FREEZE_MESSAGE}"). Variant in use: {variant} ({AE_VARIANTS[variant]}). '
        "The dev results and the decision are in docs/ae-experiment.md.",
        "",
        "Every AE cut with v2.1, z of the target site and phi under the frozen variant:",
        "",
        "| Cut | Role | Target | phi | z at target | Detected | Finding |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    ae_cuts = [
        (DEV_CUT, "dev"),
        *((c, "dev (sweep)") for c in AE_SWEEP_CUTS),
        (HOLDOUT_CUT, "step 4 holdout (seen)"),
        *(((holdout2, "holdout 2"),) if holdout2 else ()),
        *((c, "AE holdout") for c, _ in inp.ae_holdouts),
    ]
    for cut, role in ae_cuts:
        faults = [f for f in _faults(inp, cut) if f.fault_type == "ae_under_reporting"]
        e = cards.get(cut, v21)
        if not faults:
            lines.append(f"| {name(cut)} | {role} | | | | not run | |")
            continue
        fault = faults[0]
        z, phi = _ae_z(inp, cut, fault.target)
        d = (
            None
            if e is None
            else next((x for x in e.detector_matches if x.fault_id == fault.fault_id), None)
        )
        detected = "not run" if d is None else ("yes" if d.detected else "no")
        lines.append(
            f"| {name(cut)} | {role} | {fault.target} | {_f(phi)} | {_f(z)} | {detected} | "
            f"{(d.finding_id or '') if d else ''} |"
        )
    lines += [""]

    # 5. Holdouts
    holdout_sections: tuple[tuple[str, str | None], ...] = (
        ("## 5. Step 4 holdout (DC-RBQM-HOLDOUT, v2.1; seen in step 4)", HOLDOUT_CUT),
        (f"## 6. Holdout 2 ({name(holdout2) if holdout2 else 'not generated'}, v2.1)", holdout2),
    )
    for title, maybe_cut in holdout_sections:
        lines += [title, ""]
        e = cards.get(maybe_cut, v21) if maybe_cut else None
        if e is None or e.detector_metrics is None:
            lines += ["Not run yet.", ""]
            continue
        dm = e.detector_metrics
        rules = e.rules
        lines += [
            f"Detectors: recall {_f(dm.recall)}, false positives {dm.false_positives}. Rules: "
            f"{rules.agreeing if rules else 0} of {rules.units if rules else 0} units agree "
            "with condition truth.",
            "",
            *_fault_rows(e),
            "",
        ]
    if holdout2 and (inp.ground_truth_dir / f"{holdout2}.json").exists():
        lines += [
            f"Holdout 2 was generated from config/scenarios/rbqm_holdout2.toml after commit "
            f"`{inp.ae_frozen_sha}`; its results are reported once and nothing was retuned.",
            "",
        ]

    # 7. AE holdouts
    lines += ["## 7. AE holdouts (v2.1)", ""]
    if not inp.ae_holdouts:
        lines += ["Not generated yet.", ""]
    for cut, label in inp.ae_holdouts:
        e = cards.get(cut, v21)
        if e is None:
            lines += [f"* {cut} ({label}): not run.", ""]
            continue
        lines += [f"{cut} ({label}):", "", *_fault_rows(e), ""]

    # 8. Null cuts
    lines += [
        "## 8. Null cuts (v2.1)",
        "",
        "Same AE and VS data as DC-BASE, operational tables drawn with ops seeds 101 to 105.",
        "Original scoring counts every finding as a false positive; the revised scoring "
        "counts detector findings only, and checks rule findings against condition truth.",
        "",
        "| Cut | Ops seed | "
        + " | ".join(sorted(inp.agents))
        + " | All findings (original FP) | Detector FP | Natural breaches |",
        "| --- | ---: | " + " | ".join("---:" for _ in inp.agents) + " | ---: | ---: | ---: |",
    ]
    for cut, seed in (("DC-BASE", "-"), *NULL_CUTS):
        e = cards.get(cut, v21)
        cells = [_fp_by_agent(e, a) for a in sorted(inp.agents)]
        total = "not run" if e is None else str(e.metrics.overall.false_positives)
        det = _det_fp(e)
        r = _rules(e)
        lines.append(
            f"| {cut} | {seed} | {' | '.join(cells)} | {total} | "
            f"{'n/a' if det is None else det} | "
            f"{'n/a' if r is None else len(r.of('natural_breach'))} |"
        )
    lines += [""]

    # 9. Sweep
    sweep_table, smallest = _sweep(inp, cards)
    lines += [
        "## 9. Magnitude sweep (v2.1)",
        "",
        "One fault per cut, at the site it had in rbqm_core. Levels run from the weakest "
        "effect to the strongest. A planted rule fault whose level does not cross the rule's "
        "threshold is below threshold by design: neither a hit nor a miss.",
        "",
        *sweep_table,
        "",
        *smallest,
        "",
        "## 10. Sites on DC-BASE (v2 statistics)",
        "",
        *_site_table(inp),
        "",
        "## 11. Frozen thresholds and freeze order",
        "",
        f'Thresholds frozen in commit `{inp.frozen_sha}` ("{FREEZE_MESSAGE}"); the AE '
        f'variant in commit `{inp.ae_frozen_sha}` ("{AE_FREEZE_MESSAGE}").',
        "",
    ]
    if inp.freeze_violations is None:
        lines += ["Freeze order: not checked.", ""]
    elif inp.freeze_violations:
        lines += [
            "Freeze order: FAIL. Scorecards of holdout 2 or the AE holdouts written at a "
            f"commit without the AE freeze: {', '.join(inp.freeze_violations)}.",
            "",
        ]
    else:
        lines += [
            "Freeze order: every scorecard that scores holdout 2 or an AE holdout records a "
            "git commit that contains the AE freeze commit.",
            "",
        ]
    lines += [
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


def write(path: Path, inp: ReportInputs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(inp), encoding="utf-8")
    return path


__all__ = [
    "AE_FREEZE_MESSAGE",
    "NULL_CUTS",
    "ReportInputs",
    "ae_frozen_commit",
    "frozen_commit",
    "render",
    "write",
]

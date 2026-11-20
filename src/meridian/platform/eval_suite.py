"""meridian eval suite: the whole phase 1 evaluation.

Step 4 part: ingest the null cuts, inject the holdout and the sweep. Step 5 part, only
once the AE variant freeze commit exists: generate and inject holdout 2 and the three AE
holdouts. Then run the agents, score every cut (rules against condition truth, detectors
against planted faults) and write docs/eval-phase1.md. Every step is idempotent:
existing cuts, ground truth and completed agent runs are reused; each run adds new
scorecards and never changes old ones.
"""

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from meridian.data.datacut import data_cut_events
from meridian.domain.events import DataCut
from meridian.evals import report
from meridian.evals.matching import VersionFilter
from meridian.evals.rules import condition_truth
from meridian.evals.scorecard import (
    CutEvaluation,
    evaluate_cut,
    freeze_order_violations,
    git_commit,
    new_scorecard,
)
from meridian.faults.inject import GroundTruth
from meridian.faults.reader import FullCutReader
from meridian.faults.scenario import (
    AE_HOLDOUT_COUNT,
    AE_HOLDOUT_DIR,
    HOLDOUT2_FILE,
    ensure_ae_holdouts,
    ensure_holdout2,
    ensure_sweep,
    load_scenario,
    sweep_items,
)
from meridian.ledger.verify import verify
from meridian.platform.container import Container

SCENARIO_DIR = Path("config/scenarios")
REPORT_PATH = Path("docs/eval-phase1.md")


class SuiteError(Exception):
    pass


@dataclass(slots=True)
class SuiteReport:
    ingested: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    agent_runs: int = 0
    agent_failures: int = 0
    scorecards: list[Path] = field(default_factory=list)
    report_path: Path | None = None


def _score(c: Container, cuts: list[tuple[str, bool]], versions: VersionFilter) -> Path:
    evaluations: list[CutEvaluation] = []
    agents = sorted(c.registered_versions())
    kri, qtl = c.settings.agents.kri_monitor, c.settings.agents.qtl_detector
    for cut, null in cuts:
        faults = None
        if not null:
            faults = list(GroundTruth.load(c.settings.ground_truth_dir / f"{cut}.json").faults)
        conditions = condition_truth(c.study_reader(cut), kri, qtl)
        evaluations.append(evaluate_cut(c.ledger, cut, faults, versions, agents, conditions))
    card = new_scorecard(
        clock=c.clock,
        evaluations=evaluations,
        versions=versions,
        agents_config=c.settings.agents.model_dump(mode="json"),
        git_sha=git_commit(),
    )
    return c.scorecard_store().write(card)


def step5_cuts(scenario_dir: Path) -> tuple[tuple[str, str] | None, tuple[tuple[str, str], ...]]:
    """(holdout 2 (cut id, label), AE holdouts) from the scenario files that exist."""
    path = scenario_dir / HOLDOUT2_FILE
    holdout2 = None
    if path.exists():
        s = load_scenario(path)
        holdout2 = (s.out_cut, s.label or path.stem)
    ae: list[tuple[str, str]] = []
    for n in range(1, AE_HOLDOUT_COUNT + 1):
        p = scenario_dir / AE_HOLDOUT_DIR / f"{n}.toml"
        if p.exists():
            s = load_scenario(p)
            ae.append((s.out_cut, s.label or f"{AE_HOLDOUT_DIR}/{n}"))
    return holdout2, tuple(ae)


def write_report(c: Container, path: Path = REPORT_PATH, scenario_dir: Path = SCENARIO_DIR) -> Path:
    check = verify(c.ledger)
    holdout2, ae_holdouts = step5_cuts(scenario_dir)
    ae_sha = report.ae_frozen_commit()
    new_cuts = [*([holdout2[0]] if holdout2 else []), *(cut for cut, _ in ae_holdouts)]
    violations = freeze_order_violations(c.scorecard_store().all(), new_cuts, ae_sha)
    inputs = report.ReportInputs(
        store=c.scorecard_store(),
        v2_label=VersionFilter.registered(c.registered_versions()).label(),
        agents=sorted(c.registered_versions()),
        base_study=c.study_reader(report.BASE_CUT),
        ground_truth_dir=c.settings.ground_truth_dir,
        kri=c.settings.agents.kri_monitor,
        qtl=c.settings.agents.qtl_detector,
        csm=c.settings.agents.csm,
        ledger_ok=check.ok,
        ledger_events=check.events_checked,
        frozen_sha=report.frozen_commit(),
        head_sha=git_commit(),
        ae_frozen_sha=ae_sha,
        holdout2=holdout2,
        ae_holdouts=ae_holdouts,
        freeze_violations=tuple(violations) if new_cuts else None,
        study=c.study_reader,
    )
    return report.write(path, inputs)


def run_suite(
    c: Container,
    log: structlog.typing.FilteringBoundLogger,
    scenario_dir: Path = SCENARIO_DIR,
    report_path: Path = REPORT_PATH,
    ae_frozen_sha: str | None = None,
) -> SuiteReport:
    """ae_frozen_sha: the AE freeze commit; looked up in git log when not given."""
    out = SuiteReport()
    study_id = c.settings.study_id
    existing = {
        e.payload.cut_id
        for e in data_cut_events(c.ledger, study_id)
        if isinstance(e.payload, DataCut)
    }

    ingest = c.ingest_service()
    for cut, seed in report.NULL_CUTS:
        result = ingest.ingest_raw(
            c.settings.raw_dir, cut_id=cut, seed=seed, domains=c.settings.pilot.domains
        )
        if result.cut_created or cut not in existing:
            out.ingested.append(cut)

    injector = c.fault_injector()
    base = FullCutReader(c.settings.curated_root / study_id / report.BASE_CUT).read()
    core = load_scenario(scenario_dir / "rbqm_core.toml")
    scenarios = [
        scenario_dir / "rbqm_holdout.toml",
        *ensure_sweep(scenario_dir / "sweep", base, core),
    ]
    sha = ae_frozen_sha if ae_frozen_sha is not None else report.ae_frozen_commit()
    if len(sha) != 40:
        raise SuiteError(
            f'no "{report.AE_FREEZE_MESSAGE}" commit ({sha}); holdout 2 is generated only '
            "after the AE expectation variant is frozen"
        )
    # Holdout 2 must avoid the step 4 holdout's pairs, so that file has to exist first.
    injector.ensure_scenario(scenario_dir / "rbqm_holdout.toml")
    scenarios.append(ensure_holdout2(scenario_dir, base))
    scenarios += ensure_ae_holdouts(scenario_dir, base)
    for path in scenarios:
        result_i = injector.inject(path)
        if result_i.cut_created or result_i.event_appended:
            out.injected.append(result_i.manifest.cut_id)

    run = c.agent_runner(log).run_pending()
    out.agent_runs = len(run.completed)
    out.agent_failures = len(run.failed)

    holdout2, ae_holdouts = step5_cuts(scenario_dir)
    assert holdout2 is not None
    step4_faulted = [(report.DEV_CUT, False), (report.HOLDOUT_CUT, False)]
    step4_faulted += [(item.out_cut, False) for item in sweep_items()]
    clean = [(report.BASE_CUT, True), *((cut, True) for cut, _ in report.NULL_CUTS)]
    step5_faulted = [(holdout2[0], False), *((cut, False) for cut, _ in ae_holdouts)]
    registered = VersionFilter.registered(c.registered_versions())
    out.scorecards.append(_score(c, step4_faulted + step5_faulted + clean, registered))
    # v2 (every agent at 2.0.0) rescored the step 5 way on the cuts it ran on.
    v2 = VersionFilter.registered(report.V2_VERSIONS)
    out.scorecards.append(_score(c, step4_faulted + clean, v2))
    out.report_path = write_report(c, report_path, scenario_dir)
    log.info("eval.suite_done", injected=out.injected, agent_runs=out.agent_runs)
    return out

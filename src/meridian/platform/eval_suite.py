"""meridian eval suite: the whole phase 1 evaluation after the threshold freeze.

Ingest the null cuts, inject the holdout and the sweep, run the agents, score every cut
and write docs/eval-phase1.md. Every step is idempotent: existing cuts, ground truth and
completed agent runs are reused; each run adds new scorecards.
"""

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from meridian.data.datacut import data_cut_events
from meridian.domain.events import DataCut
from meridian.evals import report
from meridian.evals.matching import VersionFilter
from meridian.evals.scorecard import CutEvaluation, evaluate_cut, git_commit, new_scorecard
from meridian.faults.inject import GroundTruth
from meridian.faults.reader import FullCutReader
from meridian.faults.scenario import ensure_sweep, load_scenario, sweep_items
from meridian.ledger.verify import verify
from meridian.platform.container import Container

SCENARIO_DIR = Path("config/scenarios")
REPORT_PATH = Path("docs/eval-phase1.md")


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
    for cut, null in cuts:
        faults = None
        if not null:
            faults = list(GroundTruth.load(c.settings.ground_truth_dir / f"{cut}.json").faults)
        evaluations.append(evaluate_cut(c.ledger, cut, faults, versions, agents))
    card = new_scorecard(
        clock=c.clock,
        evaluations=evaluations,
        versions=versions,
        agents_config=c.settings.agents.model_dump(mode="json"),
        git_sha=git_commit(),
    )
    return c.scorecard_store().write(card)


def write_report(c: Container, path: Path = REPORT_PATH) -> Path:
    check = verify(c.ledger)
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
    )
    return report.write(path, inputs)


def run_suite(
    c: Container,
    log: structlog.typing.FilteringBoundLogger,
    scenario_dir: Path = SCENARIO_DIR,
    report_path: Path = REPORT_PATH,
) -> SuiteReport:
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
    for path in scenarios:
        result_i = injector.inject(path)
        if result_i.cut_created or result_i.event_appended:
            out.injected.append(result_i.manifest.cut_id)

    run = c.agent_runner(log).run_pending()
    out.agent_runs = len(run.completed)
    out.agent_failures = len(run.failed)

    v2 = VersionFilter.registered(c.registered_versions())
    faulted = [(report.DEV_CUT, False), (report.HOLDOUT_CUT, False)]
    faulted += [(item.out_cut, False) for item in sweep_items()]
    clean = [(report.BASE_CUT, True), *((cut, True) for cut, _ in report.NULL_CUTS)]
    out.scorecards.append(_score(c, faulted + clean, v2))
    out.scorecards.append(
        _score(c, [(report.DEV_CUT, False), (report.BASE_CUT, True)], VersionFilter.only("1.0.0"))
    )
    out.report_path = write_report(c, report_path)
    log.info("eval.suite_done", injected=out.injected, agent_runs=out.agent_runs)
    return out

"""Command line interface: `meridian ledger ...`."""

import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Annotated, Any

import httpx
import polars as pl
import structlog
import typer

from meridian.agents.registry import RegistrationError
from meridian.data.cut_writer import CutExistsError
from meridian.data.fetch import FetchError, fetch
from meridian.data.ingest import IngestError
from meridian.data.sites import SiteMappingError, load_site_map
from meridian.data.study_store import StudyAccessError, StudyReader
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import DataCut, Event, EventDraft, FindingRaised, RedactedEvent
from meridian.domain.findings import Evidence, Finding
from meridian.evals.matching import VersionFilter
from meridian.evals.rules import condition_truth
from meridian.evals.scorecard import (
    CutEvaluation,
    ScorecardExistsError,
    evaluate_cut,
    git_commit,
    new_scorecard,
)
from meridian.faults.catalog import FaultError
from meridian.faults.inject import GroundTruth, InjectionError
from meridian.faults.scenario import ScenarioError
from meridian.ledger.store import LedgerError
from meridian.ledger.verify import verify
from meridian.platform.clock import Clock
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox import image, policies
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.image import ImageError
from meridian.sandbox.openshell import OpenShellError

app = typer.Typer(no_args_is_help=True, help="Meridian Agent Fleet.")
ledger_app = typer.Typer(no_args_is_help=True, help="Inspect and manage the event ledger.")
faults_app = typer.Typer(no_args_is_help=True, help="Plant known faults into a copy of a cut.")
agents_app = typer.Typer(no_args_is_help=True, help="List and run oversight agents.")
eval_app = typer.Typer(no_args_is_help=True, help="Score findings against ground truth.")
findings_app = typer.Typer(no_args_is_help=True, help="Inspect findings in the ledger.")
data_app = typer.Typer(no_args_is_help=True, help="Fetch, ingest and inspect study data.")
app.add_typer(ledger_app, name="ledger")
app.add_typer(data_app, name="data")
app.add_typer(faults_app, name="faults")
app.add_typer(agents_app, name="agents")
app.add_typer(findings_app, name="findings")
app.add_typer(eval_app, name="eval")
image_app = typer.Typer(no_args_is_help=True, help="Build and inspect the agent image.")
policy_app = typer.Typer(no_args_is_help=True, help="Render the OpenShell zone policies.")
sandbox_app = typer.Typer(no_args_is_help=True, help="Run agents in OpenShell sandboxes.")
app.add_typer(image_app, name="image")
app.add_typer(policy_app, name="policy")
app.add_typer(sandbox_app, name="sandbox")

DEMO_STUDY = "MER-101"
DEMO_SITE = "SITE-007"
DEMO_ACTOR = "cli.append_demo"
DEMO_AGENT = "unblinded.efficacy_watch"


def _configure_logging(clock: Clock) -> structlog.typing.FilteringBoundLogger:
    def add_timestamp(
        _logger: object, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict["ts"] = clock.now().isoformat().replace("+00:00", "Z")
        return event_dict

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            add_timestamp,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger("meridian")
    return logger


def _container() -> Container:
    return Container.build(Settings())


@ledger_app.command("init")
def ledger_init() -> None:
    """Create the ledger database, schema and append-only triggers."""
    with _container() as c:
        log = _configure_logging(c.clock)
        c.ledger.initialize()
        log.info("ledger.initialized", path=str(c.ledger.path))
        typer.echo(f"ledger initialized at {c.ledger.path}")


@ledger_app.command("append-demo")
def ledger_append_demo() -> None:
    """Append a DataCut and one FindingRaised, for manual testing."""
    with _container() as c:
        log = _configure_logging(c.clock)
        try:
            next_seq = len(c.ledger.read(viewer_role=Role.UNBLINDED_STATISTICIAN)) + 1
            cut = c.ledger.append(
                EventDraft(
                    occurred_at=c.clock.now(),
                    actor=DEMO_ACTOR,
                    study_id=DEMO_STUDY,
                    visibility=Visibility.BLINDED,
                    payload=DataCut(
                        cut_id=f"CUT-{next_seq:04d}",
                        study_id=DEMO_STUDY,
                        path=f"data/cuts/{DEMO_STUDY}/cut-{next_seq:04d}.parquet",
                    ),
                )
            )
            finding = Finding(
                kind=FindingKind.QTL_BREACH,
                agent_id=DEMO_AGENT,
                agent_version="0.1.0",
                study_id=DEMO_STUDY,
                site_id=DEMO_SITE,
                severity=Severity.HIGH,
                title="Arm-level dropout above QTL",
                summary="Demo finding: unblinded dropout rate in one arm exceeds its QTL.",
                evidence=(
                    Evidence(
                        source=f"data_cut:{cut.seq}",
                        selector="arm=B",
                        statistic="dropout_rate",
                        value=0.21,
                        threshold=0.15,
                    ),
                ),
                visibility=Visibility.RESTRICTED,
                visible_to=frozenset({Role.UNBLINDED_STATISTICIAN}),
                score=0.87,
            )
            raised = c.ledger.append(
                EventDraft(
                    occurred_at=c.clock.now(),
                    actor=DEMO_AGENT,
                    study_id=DEMO_STUDY,
                    visibility=Visibility.RESTRICTED,
                    payload=FindingRaised(
                        finding=finding,
                        dedup_key=f"{DEMO_AGENT}:qtl_breach:{DEMO_STUDY}:cut-{cut.seq}",
                    ),
                )
            )
        except LedgerError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        assert isinstance(raised.payload, FindingRaised)
        log.info("ledger.demo_appended", seqs=[cut.seq, raised.seq])
        typer.echo(
            f"appended seq {cut.seq} (data_cut) and seq {raised.seq} "
            f"(finding_raised {raised.payload.finding.finding_id})"
        )


@ledger_app.command("show")
def ledger_show(
    since: Annotated[int, typer.Option("--since", min=0, help="Show events after this seq.")] = 0,
    role: Annotated[
        Role | None,
        typer.Option("--role", help="Viewer role. Anything but unblinded_statistician redacts."),
    ] = None,
) -> None:
    """Print events as JSON lines, redacted for the viewer role."""
    with _container() as c:
        try:
            entries = c.ledger.read(since_seq=since, viewer_role=role)
        except LedgerError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        for entry in entries:
            typer.echo(entry.model_dump_json())


@ledger_app.command("verify")
def ledger_verify() -> None:
    """Verify the hash chain. Exit code 0 when ok, 1 when broken."""
    with _container() as c:
        try:
            report = verify(c.ledger)
        except LedgerError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(report.model_dump_json(indent=2))
        if not report.ok:
            raise typer.Exit(code=1)


DATA_ERRORS = (
    FetchError,
    IngestError,
    CutExistsError,
    SiteMappingError,
    StudyAccessError,
    LedgerError,
    httpx.HTTPError,
    FileNotFoundError,
    FaultError,
    InjectionError,
    ScenarioError,
    RegistrationError,
    ScorecardExistsError,
    ImageError,
    ExecutionError,
    OpenShellError,
)


@data_app.command("fetch")
def data_fetch() -> None:
    """Download the CDISC pilot SDTM files and record their hashes."""
    with _container() as c:
        log = _configure_logging(c.clock)
        source = c.settings.pilot
        try:
            # trust_env (the default) makes httpx honour HTTPS_PROXY and friends.
            with httpx.Client(
                follow_redirects=True, timeout=source.timeout_seconds, trust_env=True
            ) as client:
                report = fetch(
                    client=client,
                    clock=c.clock,
                    base_url=source.base_url,
                    domains=source.domains,
                    raw_dir=c.settings.raw_dir,
                )
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        log.info("data.fetched", downloaded=report.downloaded, unchanged=report.unchanged)
        for name, entry in sorted(report.manifest.files.items()):
            state = "downloaded" if name in report.downloaded else "unchanged"
            typer.echo(f"{name:<8} {entry.size_bytes:>10} bytes  sha256 {entry.sha256}  {state}")


@data_app.command("ingest")
def data_ingest(
    cut: Annotated[str, typer.Option("--cut", help="Cut id, for example DC-BASE.")],
    seed: Annotated[
        int | None, typer.Option("--seed", help="Ops synthesis seed. Defaults to default_seed.")
    ] = None,
) -> None:
    """Curate the fetched pilot data into a blinded cut and record a DataCut event."""
    with _container() as c:
        log = _configure_logging(c.clock)
        resolved_seed = c.settings.default_seed if seed is None else seed
        try:
            result = c.ingest_service().ingest_raw(
                c.settings.raw_dir,
                cut_id=cut,
                seed=resolved_seed,
                domains=c.settings.pilot.domains,
            )
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        log.info(
            "data.ingested",
            cut_id=cut,
            seed=resolved_seed,
            cut_created=result.cut_created,
            event_appended=result.event_appended,
        )
        typer.echo(
            f"cut {cut} {'written' if result.cut_created else 'already present, identical'} "
            f"at {result.cut_path}"
        )
        for f in result.manifest.files:
            typer.echo(f"  {f.path:<28} {f.rows:>7} rows  sha256 {f.sha256[:16]}")
        if result.event is not None:
            verb = "appended" if result.event_appended else "already recorded"
            typer.echo(f"DataCut {verb} at ledger seq {result.event.seq}")


def _site_summary(reader: StudyReader, pilot_by_meridian: dict[str, str]) -> pl.DataFrame:
    dm = reader.table("dm").select("USUBJID", "SITEID")

    def per_site(frame: pl.DataFrame, name: str) -> pl.DataFrame:
        return frame.group_by("SITEID", maintain_order=True).agg(pl.len().alias(name))

    ae = per_site(reader.table("ae").join(dm, on="USUBJID", how="left"), "ae_count")
    vs = per_site(reader.table("vs").join(dm, on="USUBJID", how="left"), "vs_readings")
    queries = per_site(reader.table("queries").filter(pl.col("status") == "open"), "open_queries")
    deviations = per_site(reader.table("deviations"), "deviations")
    screening = (
        reader.table("screening")
        .group_by("SITEID", maintain_order=True)
        .agg((pl.col("outcome") == "screen_failure").mean().alias("screen_failure_rate"))
    )
    summary = per_site(dm, "subjects")
    for extra in (ae, vs, queries, deviations, screening):
        summary = summary.join(extra, on="SITEID", how="left")
    return (
        summary.with_columns(
            pl.col("SITEID").replace_strict(pilot_by_meridian, default=None).alias("pilot_siteid"),
            pl.col("ae_count", "vs_readings", "open_queries", "deviations").fill_null(0),
            pl.col("screen_failure_rate").round(3),
        )
        .rename({"SITEID": "site"})
        .select(
            "site",
            "pilot_siteid",
            "subjects",
            "ae_count",
            "vs_readings",
            "open_queries",
            "deviations",
            "screen_failure_rate",
        )
        .sort("site")
    )


@data_app.command("show")
def data_show(
    cut: Annotated[str, typer.Option("--cut", help="Cut id, for example DC-BASE.")],
) -> None:
    """Row counts and columns per blinded/ops table, then a per-site summary."""
    with _container() as c:
        try:
            reader = c.study_reader(cut)
            pilot_by_meridian = {m: p for p, m in load_site_map(c.settings.sites_file).items()}
            typer.echo(f"study {reader.study_id}, cut {reader.cut_id} (blinded and ops only)")
            for name in reader.tables():
                frame = reader.table(name)
                typer.echo(f"\n{name}: {frame.height} rows, {frame.width} columns")
                typer.echo("  " + ", ".join(frame.columns))
            summary = _site_summary(reader, pilot_by_meridian)
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo("\nsite summary")
        with pl.Config(
            tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, tbl_hide_dataframe_shape=True
        ):
            typer.echo(str(summary))


@faults_app.command("inject")
def faults_inject(
    scenario: Annotated[Path, typer.Option("--scenario", help="Scenario TOML file.")],
) -> None:
    """Write the faulted cut, its ground truth and a DataCut event (each once)."""
    with _container() as c:
        log = _configure_logging(c.clock)
        try:
            result = c.fault_injector().inject(scenario)
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        log.info(
            "faults.injected",
            cut_id=result.manifest.cut_id,
            cut_created=result.cut_created,
            event_appended=result.event_appended,
        )
        state = "written" if result.cut_created else "already present, identical"
        typer.echo(f"cut {result.manifest.cut_id} {state} at {result.cut_path}")
        typer.echo(f"ground truth at {result.ground_truth_path}")
        for record in result.ground_truth.faults:
            achieved = ", ".join(f"{k}={v:.4g}" for k, v in record.achieved.items())
            typer.echo(
                f"  {record.fault_id} {record.fault_type:<22} target {record.target:<6} "
                f"rows {sum(record.rows_touched.values()):>5}  {achieved}"
            )
        verb = "appended" if result.event_appended else "already recorded"
        typer.echo(f"DataCut {verb} at ledger seq {result.event.seq}")


@agents_app.command("list")
def agents_list() -> None:
    """Every known agent, whether it is enabled, and what wakes it."""
    with _container() as c:
        for registered in c.agent_registry().all():
            agent = registered.agent
            state = "enabled" if registered.enabled else "disabled"
            triggers = ",".join(sorted(t.value for t in agent.subscribes_to))
            typer.echo(
                f"{agent.agent_id:<20} v{agent.version:<7} zone {agent.zone.value:<10} "
                f"{state:<9} wakes on {triggers}"
            )


ExecutorOption = Annotated[
    str | None,
    typer.Option("--executor", help="inprocess or openshell (default: [runtime] executor)."),
]


@agents_app.command("run")
def agents_run(executor: ExecutorOption = None) -> None:
    """One runner pass over every pending (agent, DataCut) pair."""
    settings = Settings()
    if executor is not None:
        if executor not in ("inprocess", "openshell"):
            typer.echo(f"error: unknown executor {executor!r}", err=True)
            raise typer.Exit(code=2)
        runtime = settings.runtime.model_copy(update={"executor": executor})
        settings = settings.model_copy(update={"runtime": runtime})
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        try:
            report = c.agent_runner(log).run_pending()
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"completed {len(report.completed)} agent run(s), failed {len(report.failed)}, "
            f"findings appended {len(report.findings_appended)}, "
            f"skipped as duplicates {len(report.findings_skipped)}"
        )
        for agent_id, seq in report.completed:
            typer.echo(f"  done   {agent_id} on DataCut seq {seq}")
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on DataCut seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)


@findings_app.command("show")
def findings_show(
    cut: Annotated[str | None, typer.Option("--cut", help="Only findings on this cut.")] = None,
    role: Annotated[
        Role | None,
        typer.Option("--role", help="Viewer role. Restricted findings are redacted for others."),
    ] = None,
) -> None:
    """One line per finding: id, agent, kind, site, severity, score, title."""
    with _container() as c:
        try:
            entries = c.ledger.read(event_types=[EventType.FINDING_RAISED], viewer_role=role)
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        shown = 0
        for entry in entries:
            if isinstance(entry, RedactedEvent):
                if cut is None:
                    typer.echo(f"seq {entry.seq:<5} [restricted]")
                    shown += 1
                continue
            assert isinstance(entry, Event)
            assert isinstance(entry.payload, FindingRaised)
            parts = parse_dedup_key(entry.payload.dedup_key)
            if cut is not None and parts.cut_id != cut:
                continue
            f = entry.payload.finding
            typer.echo(
                f"{f.finding_id or '-':<10} {f.agent_id:<18} {f.kind.value:<17} "
                f"{f.site_id or 'STUDY':<6} {f.severity.value:<6} {f.score:>8.2f}  {f.title}"
            )
            shown += 1
        if shown == 0:
            typer.echo("no findings")


AgentVersionOption = Annotated[
    str | None,
    typer.Option(
        "--agent-version", help="Score only findings of this version (default: registered ones)."
    ),
]


def _version_filter(c: Container, version: str | None) -> VersionFilter:
    return (
        VersionFilter.only(version)
        if version
        else VersionFilter.registered(c.registered_versions())
    )


def _print_evaluation(e: CutEvaluation) -> None:
    m = e.metrics.overall
    recall = "n/a" if m.recall is None else f"{m.recall:.2f}"
    ap = "n/a" if m.average_precision is None else f"{m.average_precision:.2f}"
    typer.echo(
        f"{e.cut_id}: {m.findings} findings, recall {recall}, false positives "
        f"{m.false_positives}, average precision {ap}"
    )
    for match in e.matches:
        hit = f"{match.finding_id} (score {match.score:.2f})" if match.detected else "MISSED"
        dup = f", duplicates {', '.join(match.duplicates)}" if match.duplicates else ""
        typer.echo(f"  {match.fault_id} {match.fault_type:<22} {match.target:<6} {hit}{dup}")
    for agent_id, am in e.metrics.per_agent.items():
        typer.echo(
            f"  {agent_id:<20} findings {am.findings:>3}  false positives {am.false_positives}"
        )
    if e.detector_metrics is not None:
        dm = e.detector_metrics
        d_recall = "n/a" if dm.recall is None else f"{dm.recall:.2f}"
        typer.echo(f"  detectors: recall {d_recall}, false positives {dm.false_positives}")
    if e.rules is not None:
        typer.echo(
            f"  rules: {e.rules.agreeing} of {e.rules.units} units agree with condition truth"
        )
        for o in e.rules.outcomes:
            typer.echo(f"    {o.check:<26} {o.site:<6} {o.outcome}")


def _score(cut: str, version: str | None, null: bool) -> None:
    with _container() as c:
        try:
            faults = None
            if not null:
                truth_path = c.settings.ground_truth_dir / f"{cut}.json"
                if not truth_path.exists():
                    typer.echo(f"error: no ground truth at {truth_path}", err=True)
                    raise typer.Exit(code=1)
                faults = list(GroundTruth.load(truth_path).faults)
            versions = _version_filter(c, version)
            conditions = condition_truth(
                c.study_reader(cut),
                c.settings.agents.kri_monitor,
                c.settings.agents.qtl_detector,
            )
            evaluation = evaluate_cut(
                c.ledger, cut, faults, versions, sorted(c.registered_versions()), conditions
            )
            card = new_scorecard(
                clock=c.clock,
                evaluations=[evaluation],
                versions=versions,
                agents_config=c.settings.agents.model_dump(mode="json"),
                git_sha=git_commit(),
            )
            path = c.scorecard_store().write(card)
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        _print_evaluation(evaluation)
        typer.echo(f"scorecard {path} ({versions.label()})")


@eval_app.command("run")
def eval_run(
    cut: Annotated[str, typer.Option("--cut", help="Cut with a ground truth file.")],
    agent_version: AgentVersionOption = None,
) -> None:
    """Score findings on a faulted cut against its ground truth and write a scorecard."""
    _score(cut, agent_version, null=False)


@eval_app.command("null")
def eval_null(
    cut: Annotated[str, typer.Option("--cut", help="A clean cut: every finding is a FP.")],
    agent_version: AgentVersionOption = None,
) -> None:
    """Score a clean cut, where every finding is a false positive, and write a scorecard."""
    _score(cut, agent_version, null=True)


@eval_app.command("suite")
def eval_suite() -> None:
    """After the freeze: null cuts, holdout, sweep, agents, scorecards and the report."""
    from meridian.platform.eval_suite import run_suite

    with _container() as c:
        log = _configure_logging(c.clock)
        try:
            result = run_suite(c, log)
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"ingested: {', '.join(result.ingested) or 'nothing new'}")
        typer.echo(f"injected: {', '.join(result.injected) or 'nothing new'}")
        typer.echo(f"agent runs: {result.agent_runs}, failures: {result.agent_failures}")
        for path in result.scorecards:
            typer.echo(f"scorecard {path}")
        typer.echo(f"report {result.report_path}")
        if result.agent_failures:
            raise typer.Exit(code=1)


@eval_app.command("report")
def eval_report() -> None:
    """Write docs/eval-phase1.md from the latest scorecards."""
    from meridian.platform.eval_suite import write_report

    with _container() as c:
        try:
            path = write_report(c)
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"wrote {path}")


@image_app.command("build")
def image_build(
    tag: Annotated[str | None, typer.Option("--tag", help="Default: [runtime] image.")] = None,
) -> None:
    """docker build the agent image from docker/agent.Dockerfile and print its digest."""
    with _container() as c:
        ref = tag or c.image_ref()
        try:
            info = image.build(ref)
        except ImageError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(f"image {info.ref}")
    typer.echo(f"digest {info.digest}")
    typer.echo(f"size {info.size_bytes} bytes ({info.size_bytes / 1e6:.1f} MB), user {info.user}")


@policy_app.command("render")
def policy_render() -> None:
    """Write policies/<zone>.yaml for every zone and policies/boundaries/<name>.yaml."""
    root = Settings().runtime.policies_dir
    for path in policies.render_all(root):
        typer.echo(f"wrote {path} (sha256 {policies.sha256_text(path.read_text())})")


@policy_app.command("prove")
def policy_prove(
    prover: Annotated[
        str, typer.Option("--prover", help="openshell-prover binary.")
    ] = "openshell-prover",
) -> None:
    """Check every zone policy against its boundary with openshell-prover."""
    from meridian.sandbox.prover import ProverError, prove_all

    root = Settings().runtime.policies_dir
    try:
        results = prove_all(root, prover)
    except ProverError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for zone, result in results.items():
        verdict = "pass" if result.passed else "FAIL"
        typer.echo(f"{verdict}  {zone:<12} vs {result.boundary}: {result.explain()}")
    if not all(r.passed for r in results.values()):
        raise typer.Exit(code=1)


@sandbox_app.command("verify")
def sandbox_verify(
    cuts: Annotated[
        list[str] | None, typer.Option("--cut", help="Repeatable. Default: DC-BASE, DC-RBQM-CORE.")
    ] = None,
) -> None:
    """Run every enabled agent in process and in a sandbox; compare drafts byte for byte."""
    from meridian.sandbox.verify import verify

    with _container() as c:
        log = _configure_logging(c.clock)
        try:
            rows = verify(
                ledger=c.ledger,
                study_id=c.settings.study_id,
                registry=c.agent_registry(),
                inprocess=c.inprocess_executor(log),
                sandboxed=c.openshell_executor(log),
                cuts=cuts or ["DC-BASE", "DC-RBQM-CORE"],
            )
        except (*DATA_ERRORS, LookupError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo("| Agent | Cut | Identical | Drafts | Sandbox ms | In-process ms |")
    typer.echo("| --- | --- | --- | ---: | ---: | ---: |")
    for r in rows:
        typer.echo(
            f"| {r.agent_id} | {r.cut_id} | {'yes' if r.identical else 'NO'} | {r.drafts} | "
            f"{r.sandbox_ms} | {r.inprocess_ms} |"
        )
    if not all(r.identical for r in rows):
        raise typer.Exit(code=1)


@sandbox_app.command("reap")
def sandbox_reap() -> None:
    """Delete every sandbox in var/sandboxes.jsonl that is not yet deleted, and only those."""
    from meridian.sandbox.openshell import SubprocessOpenShellClient
    from meridian.sandbox.tracking import TrackingError, reap

    with _container() as c:
        log = _configure_logging(c.clock)
        tracker = c.sandbox_tracker()
        client = SubprocessOpenShellClient()
        try:
            report = reap(
                tracker=tracker,
                delete=client.delete,
                live=client.names,
                log=log,
                confirm_seconds=c.settings.runtime.delete_confirm_seconds,
            )
        except (TrackingError, OpenShellError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(
        f"reaped {len(report.deleted)}, already gone {len(report.already_gone)}, "
        f"failed {len(report.failed)} (tracked in {tracker.path})"
    )
    for name in report.deleted:
        typer.echo(f"  deleted      {name}")
    for name in report.already_gone:
        typer.echo(f"  already gone {name}")
    for name in report.failed:
        typer.echo(f"  FAILED       {name}")
    if report.failed:
        raise typer.Exit(code=1)

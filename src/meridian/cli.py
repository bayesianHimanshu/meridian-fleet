"""Command line interface: `meridian ledger ...`."""

import json
import sys
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import httpx
import polars as pl
import structlog
import typer

from meridian.agents.registry import RegistrationError
from meridian.agents.runner import SandboxRequiredError
from meridian.data.cut_writer import CutExistsError
from meridian.data.fetch import FetchError, fetch
from meridian.data.ingest import IngestError
from meridian.data.sites import SiteMappingError, load_site_map
from meridian.data.study_store import StudyAccessError, StudyReader
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import (
    DataCut,
    Event,
    EventDraft,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    RedactedEvent,
)
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

if TYPE_CHECKING:
    from fastapi import FastAPI

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
api_app = typer.Typer(no_args_is_help=True, help="Serve the Fleet API (development identity).")
app.add_typer(api_app, name="api")
safety_app = typer.Typer(
    no_args_is_help=True, help="The synthetic safety database and SAE reconciliation."
)
app.add_typer(safety_app, name="safety")
tmf_app = typer.Typer(
    no_args_is_help=True,
    help="The synthetic eTMF index and quality.tmf_completeness (TMF completeness).",
)
app.add_typer(tmf_app, name="tmf")
audit_app = typer.Typer(
    no_args_is_help=True, help="Review the audit trail (quality.audit_trail_reviewer)."
)
app.add_typer(audit_app, name="audit")

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
    typer.Option(
        "--executor",
        help=(
            "zoned (the default, [runtime] executor: per policy, [runtime.executors]), "
            "openshell or inprocess. inprocess runs agent "
            "code outside any sandbox and is refused for operational cuts (DC-BASE)."
        ),
    ),
]


@agents_app.command("run")
def agents_run(
    executor: ExecutorOption = None,
    evaluation_only: Annotated[
        bool,
        typer.Option(
            "--evaluation-only",
            help="Only evaluation cuts (operational cuts stay pending; they need openshell).",
        ),
    ] = False,
    only_cuts: Annotated[
        list[str] | None, typer.Option("--cut", help="Only these cuts; repeatable.")
    ] = None,
    rerun: Annotated[
        bool,
        typer.Option(
            "--rerun",
            help="With --cut: also run pairs already completed (findings dedup as skipped).",
        ),
    ] = False,
) -> None:
    """One runner pass over every pending (agent, DataCut) pair."""
    from meridian.data.datacut import data_cut_events
    from meridian.domain.events import DataCut

    settings = Settings()
    if executor is not None:
        if executor not in ("inprocess", "openshell", "zoned"):
            typer.echo(f"error: unknown executor {executor!r}", err=True)
            raise typer.Exit(code=2)
        runtime = settings.runtime.model_copy(update={"executor": executor})
        settings = settings.model_copy(update={"runtime": runtime})
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        cuts = None
        if evaluation_only:
            cuts = {
                e.payload.cut_id
                for e in data_cut_events(c.ledger, settings.study_id)
                if isinstance(e.payload, DataCut)
                and e.payload.cut_id not in settings.cuts.operational
            }
        if rerun and not only_cuts:
            typer.echo("error: --rerun needs --cut", err=True)
            raise typer.Exit(code=2)
        if only_cuts:
            cuts = set(only_cuts) if cuts is None else cuts & set(only_cuts)
        started = time.perf_counter()
        try:
            report = c.agent_runner(log).run_pending(cuts=cuts, rerun=rerun)
        except SandboxRequiredError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        except DATA_ERRORS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"completed {len(report.completed)} agent run(s), failed {len(report.failed)}, "
            f"findings appended {len(report.findings_appended)}, "
            f"skipped as duplicates {len(report.findings_skipped)}"
        )
        per = ", ".join(
            f"{n} {k} ({report.capacity.get(n, 1)} at a time)"
            for n, k in sorted(report.executors.items())
        )
        typer.echo(f"  {time.perf_counter() - started:.1f} s; runs per executor: {per or 'none'}")
        for agent_id, seq in report.completed:
            typer.echo(f"  done   {agent_id} on DataCut seq {seq}")
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on DataCut seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)


def _policy_conforms(c: Container, name: str) -> str | None:
    """None if the latest conformance run of the agent policy passed with the committed
    policy; otherwise why its agents may not run."""
    from meridian.domain.events import ContainmentCheckCompleted

    latest = None
    for entry in c.ledger.read(event_types=[EventType.CONTAINMENT_CHECK_COMPLETED]):
        if (
            isinstance(entry, Event)
            and isinstance(entry.payload, ContainmentCheckCompleted)
            and entry.payload.policy_name == name
        ):
            latest = entry.payload
    if latest is None:
        return f"no conformance run of {name} in the ledger"
    policy = policies.ALL_POLICIES[name]
    if not latest.passed or (policy.providers and latest.credential_value_visible is not False):
        return f"the latest conformance run of {name} ({latest.sandbox}) did not pass"
    if latest.policy_sha256 != policies.sha256_text(policies.expected_effective(policy)):
        return f"{name} changed since its latest conformance run"
    return None


def _narrator_policy_conforms(c: Container) -> str | None:
    return _policy_conforms(c, policies.NARRATOR_POLICY)


def _require_local_model_policy(c: Container) -> None:
    problem = _policy_conforms(c, policies.LOCAL_MODEL_POLICY)
    if problem:
        typer.echo(
            f"error: {problem}; run meridian sandbox conformance --policy "
            f"{policies.LOCAL_MODEL_POLICY}",
            err=True,
        )
        raise typer.Exit(code=1)


def _local_settings(max_calls: int | None) -> Settings:
    settings = Settings()
    if settings.local_model is None:
        typer.echo("error: no [local_model] in the configuration", err=True)
        raise typer.Exit(code=1)
    if max_calls is not None:
        local = settings.local_model.model_copy(update={"max_calls_per_pass": max_calls})
        settings = settings.model_copy(update={"local_model": local})
    return settings


@agents_app.command("adjudicate")
def agents_adjudicate(
    cuts: Annotated[
        list[str] | None, typer.Option("--cut", help="Only findings of these cuts; repeatable.")
    ] = None,
    max_calls: Annotated[
        int | None,
        typer.Option("--max-calls", help="Default: [local_model] max_calls_per_pass.", min=1),
    ] = None,
) -> None:
    """One safety.term_adjudicator pass in the safety.local_model sandbox."""
    with Container.build(_local_settings(max_calls)) as c:
        log = _configure_logging(c.clock)
        _require_local_model_policy(c)
        try:
            report = c.adjudicator_runner(log).run_pass(cuts)
        except (ImageError, OpenShellError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    counts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items())) or "nothing"
    typer.echo(
        f"assessed: {counts}; over budget: {len(report.deferred)}; not sent: {len(report.skipped)}"
    )
    if report.batch_error:
        typer.echo(f"  batch failed: {report.batch_error}")
    for event in report.events:
        p = event.payload
        assert isinstance(p, FindingAssessment)
        detail = (
            f"{p.verdict}: {p.reason}"
            if p.status == "accepted"
            else ", ".join(p.rejection_reasons) or p.error
        )
        typer.echo(f"  seq {event.seq} {'+'.join(p.finding_ids)} {p.status:<8} {detail}")
        for a in p.answers:
            said = a.verdict or ",".join(a.rejection_reasons) or a.error
            typer.echo(f"    {a.model} ({(a.model_digest or '')[:12]}) {a.status}: {said}")


@agents_app.command("narrate-local")
def agents_narrate_local(
    cuts: Annotated[
        list[str] | None, typer.Option("--cut", help="Only findings of these cuts; repeatable.")
    ] = None,
    max_calls: Annotated[
        int | None,
        typer.Option("--max-calls", help="Default: [local_model] max_calls_per_pass.", min=1),
    ] = None,
) -> None:
    """One safety.local_narrator pass in the safety.local_model sandbox."""
    with Container.build(_local_settings(max_calls)) as c:
        log = _configure_logging(c.clock)
        _require_local_model_policy(c)
        try:
            report = c.local_narrator_runner(log).run_pass(cuts)
        except (ImageError, OpenShellError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    counts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items())) or "nothing"
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(report.reasons.items())) or "none"
    typer.echo(
        f"narrated: {counts}; rejection reasons: {reasons}; over budget: {len(report.deferred)}"
    )
    if report.batch_error:
        typer.echo(f"  batch failed: {report.batch_error}")
    for event in report.events:
        p = event.payload
        assert isinstance(p, FindingNarrative)
        detail = p.headline if p.status == "accepted" else ", ".join(p.rejection_reasons)
        typer.echo(f"  seq {event.seq} {p.finding_id} {p.status:<8} {detail or p.error}")


@agents_app.command("narrate")
def agents_narrate(
    cuts: Annotated[
        list[str] | None, typer.Option("--cut", help="Only findings of these cuts; repeatable.")
    ] = None,
    max_calls: Annotated[
        int | None,
        typer.Option("--max-calls", help="Default: [narrator] max_calls_per_pass.", min=1),
    ] = None,
) -> None:
    """One rbqm.narrator pass in an OpenShell sandbox with the meridian-anthropic provider."""
    settings = Settings()
    if max_calls is not None:
        narr = settings.narrator.model_copy(update={"max_calls_per_pass": max_calls})
        settings = settings.model_copy(update={"narrator": narr})
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        problem = _narrator_policy_conforms(c)
        if problem:
            typer.echo(
                f"error: {problem}; run meridian sandbox conformance --policy rbqm.narrator",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            report = c.narrator_runner(log).run_pass(cuts)
        except (ImageError, OpenShellError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    counts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items())) or "nothing"
    typer.echo(f"narrated: {counts}; over budget, for the next pass: {len(report.deferred)}")
    if report.batch_error:
        typer.echo(f"  batch failed: {report.batch_error}")
    for event in report.events:
        p = event.payload
        assert isinstance(p, FindingNarrative)
        detail = p.headline if p.status == "accepted" else ", ".join(p.rejection_reasons)
        typer.echo(f"  seq {event.seq} {p.finding_id} {p.status:<8} {detail or p.error}")


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


@eval_app.command("terms")
def eval_terms(
    file: Annotated[
        Path, typer.Option("--file", help="Term pairs, JSON lines (docs/local-model-eval.md).")
    ] = Path("data/term_pairs/dev.jsonl"),
    models: Annotated[
        list[str] | None,
        typer.Option("--model", help="Pinned model to run; repeatable. Default: every one."),
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the per-pair results (JSON).")
    ] = Path("var/evals/term_pairs.json"),
    sealed: Annotated[
        bool,
        typer.Option("--sealed", help="A sealed set: print the metrics and errors, write nothing."),
    ] = False,
) -> None:
    """safety.term_adjudicator on labelled term pairs, per model, in the local model sandbox."""
    from meridian.agents.safety import term_eval as tp

    settings = _local_settings(None)
    local = settings.local_model
    assert local is not None
    pairs = tp.load_pairs(file)
    names = models or sorted(local.digests)
    unknown = sorted(set(names) - set(local.digests))
    if unknown:
        typer.echo(f"error: not pinned in [local_model.digests]: {unknown}", err=True)
        raise typer.Exit(code=1)
    reports = []
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        _require_local_model_policy(c)
        for name in names:
            cfg = local.model_copy(update={"model": name, "max_calls_per_pass": len(pairs)})
            report = tp.run_model(pairs, cfg, c.local_model_executor(log))
            report.placement = tp.placement(cfg.base_url, name)
            reports.append(report)
            typer.echo(json.dumps(report.summary(), sort_keys=True))
            for r in report.results:
                if not r.correct:
                    typer.echo(
                        f"  {r.pair.id} {r.pair.category} label {r.pair.label}: "
                        f"{r.status} {r.verdict or ','.join(r.rejection_reasons) or r.error}"
                        + (
                            f" ({r.pair.record_a.preferred_term} / "
                            f"{r.pair.record_b.preferred_term})"
                            if sealed
                            else ""
                        )
                    )
    chosen = tp.choose(reports)
    typer.echo(f"chosen by the rule: {chosen.model if chosen else 'none'}")
    if sealed:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "file": file.as_posix(),
                "models": [r.summary() for r in reports],
                "chosen": chosen.model if chosen else None,
                "results": {
                    r.model: [
                        {
                            "id": x.pair.id,
                            "category": x.pair.category,
                            "label": x.pair.label,
                            "status": x.status,
                            "verdict": x.verdict,
                            "reason": x.reason,
                            "rejection_reasons": list(x.rejection_reasons),
                            "latency_ms": x.latency_ms,
                        }
                        for x in r.results
                    ]
                    for r in reports
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    typer.echo(f"wrote {out}")


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
    """Write every policy, policies/boundaries/<name>.yaml and profiles/<provider>.yaml."""
    root = Settings().runtime.policies_dir
    for path in [*policies.render_all(root), *policies.render_profiles()]:
        typer.echo(f"wrote {path} (sha256 {policies.sha256_text(path.read_text())})")


@policy_app.command("prove")
def policy_prove(
    prover: Annotated[
        str, typer.Option("--prover", help="openshell-prover binary.")
    ] = "openshell-prover",
) -> None:
    """Check every committed policy against its boundary with openshell-prover."""
    from meridian.sandbox.prover import ProverError, prove_all

    root = Settings().runtime.policies_dir
    try:
        results = prove_all(root, prover)
    except ProverError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for zone, result in results.items():
        verdict = "pass" if result.passed else "FAIL"
        typer.echo(f"{verdict}  {zone:<13} vs {result.boundary}: {result.explain()}")
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


@sandbox_app.command("conformance")
def sandbox_conformance(
    zones: Annotated[
        list[str] | None, typer.Option("--zone", help="Zone to check; repeatable, or 'all'.")
    ] = None,
    policy_names: Annotated[
        list[str] | None,
        typer.Option("--policy", help="An agent policy to check (rbqm.narrator); repeatable."),
    ] = None,
    executor: Annotated[
        str,
        typer.Option(
            "--executor",
            help="openshell, or substrate: the same zones in Agent Substrate actors "
            "(meridian substrate conformance).",
        ),
    ] = "openshell",
) -> None:
    """Check committed policies in real sandboxes and record the results."""
    from meridian.sandbox.conformance import ledger_drafts, run_conformance
    from meridian.sandbox.openshell import SubprocessOpenShellClient

    if executor == "substrate":
        from meridian.cli_substrate import substrate_conformance

        chosen = list(policy_names or []) + ([] if zones == ["all"] else list(zones or []))
        substrate_conformance(zones=chosen or None, record=True)
        return
    if executor != "openshell":
        typer.echo(f"error: unknown executor {executor!r}", err=True)
        raise typer.Exit(code=2)

    selected = sorted(policies.POLICIES) if zones == ["all"] else list(zones or [])
    selected += list(policy_names or [])
    unknown = sorted(set(selected) - set(policies.ALL_POLICIES))
    if unknown or not selected:
        typer.echo(
            f"error: unknown or no policy {unknown}; known: {sorted(policies.ALL_POLICIES)}",
            err=True,
        )
        raise typer.Exit(code=2)
    failed = []
    with _container() as c:
        log = _configure_logging(c.clock)
        rt = c.settings.runtime
        ref = c.image_ref()
        try:
            digest = image.inspect(ref).digest
            for zone in selected:
                result = run_conformance(
                    client=SubprocessOpenShellClient(),
                    zone=zone,
                    image=ref,
                    image_digest=digest,
                    policies_dir=rt.policies_dir,
                    tracker=c.sandbox_tracker(),
                    log=log,
                    log_timeout_seconds=rt.conformance_log_timeout_seconds,
                    delete_confirm_seconds=rt.delete_confirm_seconds,
                )
                events = [c.ledger.append(d) for d in ledger_drafts(result, c.clock)]
                verdict = "pass" if result.passed else "FAIL"
                typer.echo(
                    f"{verdict}  {zone} (sandbox {result.sandbox}, ledger seq "
                    f"{events[0].seq}-{events[-1].seq}, log line after {result.log_wait_ms} ms)"
                )
                for check in result.checks:
                    typer.echo(
                        f"  {check.name:<24} expect {check.expect:<8} got {check.outcome:<12} "
                        f"exit {check.exit_code}  {check.message}"
                    )
                if result.providers:
                    typer.echo(
                        f"  providers {', '.join(result.providers)}; ANTHROPIC_API_KEY in the "
                        f"sandbox starts with sk-ant-: {result.credential_value_visible}"
                    )
                for record in result.log_records:
                    typer.echo(f"  log: {record.raw}")
                for record in result.allowed_records:
                    typer.echo(f"  log (allowed): {record.raw}")
                if result.malformed_lines:
                    typer.echo(f"  malformed NET:OPEN lines: {len(result.malformed_lines)}")
                for failure in result.failures():
                    typer.echo(f"  FAIL: {failure}")
                if not result.passed:
                    failed.append(zone)
        except (ImageError, OpenShellError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if failed:
        raise typer.Exit(code=1)


def _api(settings: Settings, clock: Clock) -> FastAPI:
    from meridian.api.app import context_from_settings, create_app

    ctx = context_from_settings(
        settings, clock, settings.api.people_file, settings.api.portfolio_file
    )
    return create_app(ctx)


def openapi_text(settings: Settings) -> str:
    """The OpenAPI document as committed: sorted keys, two-space indent, final newline."""
    import json

    from meridian.platform.clock import SystemClock

    schema = _api(settings, SystemClock()).openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


@api_app.command("serve")
def api_serve(
    port: Annotated[int | None, typer.Option("--port", help="Default: [api] port.")] = None,
) -> None:
    """Run the Fleet API with uvicorn on 127.0.0.1 (never another interface)."""
    import uvicorn

    from meridian.api.app import HOST
    from meridian.platform.clock import SystemClock

    settings = Settings()
    if not settings.ledger_path.exists():
        typer.echo(f"error: no ledger at {settings.ledger_path}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"Fleet API on http://{HOST}:{port or settings.api.port}/api/v1 "
        "(development identity, not authentication)"
    )
    uvicorn.run(_api(settings, SystemClock()), host=HOST, port=port or settings.api.port)


@api_app.command("openapi")
def api_openapi() -> None:
    """Write the OpenAPI document to [api] openapi_file (openapi/meridian-api.json)."""
    settings = Settings()
    path = settings.api.openapi_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(openapi_text(settings), encoding="utf-8")
    typer.echo(f"wrote {path}")


SetOption = Annotated[str, typer.Option("--set", help="dev, holdout, clean or terms.")]


def _set_name(name: str) -> Literal["dev", "holdout", "clean", "terms"]:
    if name not in ("dev", "holdout", "clean", "terms"):
        typer.echo(f"error: unknown set {name!r}; dev, holdout, clean or terms", err=True)
        raise typer.Exit(code=2)
    return name  # type: ignore[return-value]


@safety_app.command("generate")
def safety_generate() -> None:
    """A clean safety database snapshot for every recorded cut that has none yet."""
    from meridian.data.datacut import data_cut_events
    from meridian.domain.events import DataCut
    from meridian.faults.reader import FullCutReader
    from meridian.safetydb.generate import ensure_clean_snapshot

    with _container() as c:
        s = c.settings
        study_dir = s.curated_root / s.study_id
        written = present = 0
        for event in data_cut_events(c.ledger, s.study_id):
            assert isinstance(event.payload, DataCut)
            cut_id = event.payload.cut_id
            if (s.safety_root / s.study_id / cut_id / "manifest.json").exists():
                present += 1
                continue
            if not (study_dir / cut_id).is_dir():
                typer.echo(f"  skipped {cut_id}: no curated cut on disk")
                continue
            ensure_clean_snapshot(
                safety_root=s.safety_root,
                study_id=s.study_id,
                cut=FullCutReader(study_dir / cut_id).read(),
                clock=c.clock,
            )
            written += 1
            typer.echo(f"  wrote clean snapshot for {cut_id}")
        typer.echo(f"clean snapshots written {written}, already present {present}")


@safety_app.command("inject")
def safety_inject(set_name: SetOption) -> None:
    """Write one evaluation set: cuts, safety snapshots, ground truth, DataCut events."""
    name = _set_name(set_name)
    with _container() as c:
        results = c.safety_injector().inject_set(name)
        for r in results:
            planted = ", ".join(f"{f.kri}:{f.target}" for f in r.ground_truth.faults) or "none"
            state = "written" if r.cut_created else "present"
            typer.echo(
                f"{r.scenario.label} {r.scenario.cut_id} {state} seq {r.event.seq}: {planted}"
            )


@safety_app.command("run")
def safety_run(
    executor: ExecutorOption = None,
    set_name: Annotated[
        str | None,
        typer.Option("--set", help="dev, holdout, clean or terms; default DC-BASE."),
    ] = None,
) -> None:
    """Run safety.sae_reconciler on DC-BASE or on one evaluation set only."""
    from meridian.agents.safety.sae_reconciler import AGENT_ID
    from meridian.evals.safety import set_cuts

    settings = Settings()
    if executor is not None:
        runtime = settings.runtime.model_copy(update={"executor": executor})
        settings = settings.model_copy(update={"runtime": runtime})
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        cuts = (
            ["DC-BASE"]
            if set_name is None
            else set_cuts(settings.ground_truth_dir, _set_name(set_name))
        )
        try:
            report = c.agent_runner(log).run_pending(agents={AGENT_ID}, cuts=set(cuts))
        except SandboxRequiredError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(
            f"completed {len(report.completed)} run(s) on {len(cuts)} cut(s), failed "
            f"{len(report.failed)}, findings appended {len(report.findings_appended)}"
        )
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on DataCut seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)


@safety_app.command("eval")
def safety_eval(
    set_name: Annotated[
        str, typer.Option("--set", help="dev, holdout, clean, terms or base (DC-BASE).")
    ],
) -> None:
    """Precision and recall per check from the ledger; writes data/evals/safety/<set>.json."""
    from meridian.agents.safety.sae_reconciler import AGENT_ID, VERSION
    from meridian.evals.safety import evaluate, set_cuts, table

    with _container() as c:
        s = c.settings
        cuts = (
            ["DC-BASE"] if set_name == "base" else set_cuts(s.ground_truth_dir, _set_name(set_name))
        )
        result = evaluate(
            name=set_name,
            cuts=cuts,
            ledger=c.ledger,
            ground_truth_dir=s.ground_truth_dir,
            safety_study_root=s.safety_root / s.study_id,
            agent_id=AGENT_ID,
            agent_version=VERSION,
        )
        # Its own folder: data/evals/*.json are RBQM scorecards.
        out = s.evals_dir / "safety" / f"{set_name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json(), encoding="utf-8")
        typer.echo(table([result]))
        typer.echo(
            f"cuts {len(cuts)}, without a run {len(result.runs_missing)}, "
            f"findings {result.findings}"
        )
        typer.echo(f"harmless present {dict(result.harmless_present)}")
        typer.echo(f"harmless flagged {dict(result.harmless_flagged)}")
        for cut, check, record in result.false:
            typer.echo(f"  false  {cut} {check} {record}")
        for cut, check, record in result.missed:
            typer.echo(f"  missed {cut} {check} {record}")
        typer.echo(f"written to {out}")


TmfSetOption = Annotated[str, typer.Option("--set", help="dev, holdout or clean.")]


def _tmf_set(name: str) -> Literal["dev", "holdout", "clean"]:
    if name not in ("dev", "holdout", "clean"):
        typer.echo(f"error: unknown set {name!r}; dev, holdout or clean", err=True)
        raise typer.Exit(code=2)
    return name  # type: ignore[return-value]


@tmf_app.command("generate")
def tmf_generate() -> None:
    """A clean eTMF snapshot for every recorded cut that has none yet (DC-BASE included)."""
    from meridian.data.datacut import data_cut_events
    from meridian.domain.events import DataCut
    from meridian.faults.reader import FullCutReader
    from meridian.tmf.generate import ensure_clean_snapshot

    with _container() as c:
        s = c.settings
        study_dir = s.curated_root / s.study_id
        written = present = 0
        for event in data_cut_events(c.ledger, s.study_id):
            assert isinstance(event.payload, DataCut)
            cut_id = event.payload.cut_id
            if (s.etmf_root / s.study_id / cut_id / "manifest.json").exists():
                present += 1
                continue
            if not (study_dir / cut_id).is_dir():
                typer.echo(f"  skipped {cut_id}: no curated cut on disk")
                continue
            ensure_clean_snapshot(
                etmf_root=s.etmf_root,
                study_id=s.study_id,
                cut=FullCutReader(study_dir / cut_id).read(),
                ref=s.agents.tmf_completeness.reference,
                clock=c.clock,
            )
            written += 1
            typer.echo(f"  wrote clean eTMF snapshot for {cut_id}")
        typer.echo(f"clean eTMF snapshots written {written}, already present {present}")


@tmf_app.command("inject")
def tmf_inject(set_name: TmfSetOption) -> None:
    """Write one TMF evaluation set: cuts, eTMF snapshots, ground truth, DataCut events."""
    name = _tmf_set(set_name)
    with _container() as c:
        for r in c.tmf_injector().inject_set(name):
            planted = ", ".join(f"{f.kri}:{f.target}" for f in r.ground_truth.faults) or "none"
            state = "written" if r.cut_created else "present"
            typer.echo(
                f"{r.scenario.label} {r.scenario.cut_id} {state} seq {r.event.seq}: {planted}"
            )


@tmf_app.command("run")
def tmf_run(
    executor: ExecutorOption = None,
    set_name: Annotated[
        str | None,
        typer.Option("--set", help="dev, holdout or clean; default DC-BASE."),
    ] = None,
) -> None:
    """Run quality.tmf_completeness on DC-BASE or on one TMF evaluation set only."""
    from meridian.agents.quality.tmf_completeness import AGENT_ID
    from meridian.faults.tmf import set_cuts

    settings = Settings()
    if executor is not None:
        runtime = settings.runtime.model_copy(update={"executor": executor})
        settings = settings.model_copy(update={"runtime": runtime})
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        cuts = (
            ["DC-BASE"]
            if set_name is None
            else set_cuts(settings.ground_truth_dir, _tmf_set(set_name))
        )
        try:
            report = c.agent_runner(log).run_pending(agents={AGENT_ID}, cuts=set(cuts))
        except SandboxRequiredError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(
            f"completed {len(report.completed)} run(s) on {len(cuts)} cut(s), failed "
            f"{len(report.failed)}, findings appended {len(report.findings_appended)}"
        )
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on DataCut seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)


@tmf_app.command("eval")
def tmf_eval(
    set_name: Annotated[str, typer.Option("--set", help="dev, holdout, clean or base (DC-BASE).")],
) -> None:
    """Precision and recall per check from the ledger; writes data/evals/tmf/<set>.json."""
    from meridian.agents.quality.tmf_completeness import AGENT_ID, VERSION
    from meridian.evals.tmf import evaluate, table
    from meridian.faults.tmf import set_cuts

    with _container() as c:
        s = c.settings
        cuts = (
            ["DC-BASE"] if set_name == "base" else set_cuts(s.ground_truth_dir, _tmf_set(set_name))
        )
        result = evaluate(
            name=set_name,
            cuts=cuts,
            ledger=c.ledger,
            ground_truth_dir=s.ground_truth_dir,
            etmf_study_root=s.etmf_root / s.study_id,
            agent_id=AGENT_ID,
            agent_version=VERSION,
        )
        out = s.evals_dir / "tmf" / f"{set_name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.to_json(), encoding="utf-8")
        typer.echo(table([result]))
        typer.echo(
            f"cuts {len(cuts)}, without a run {len(result.runs_missing)}, "
            f"findings {result.findings}"
        )
        typer.echo(f"harmless present {dict(result.harmless_present)}")
        typer.echo(f"harmless flagged {dict(result.harmless_flagged)}")
        for cut, check, record in result.false:
            typer.echo(f"  false  {cut} {check} {record}")
        for cut, check, record in result.missed:
            typer.echo(f"  missed {cut} {check} {record}")
        typer.echo(f"written to {out}")


@audit_app.command("review")
def audit_review(
    through_seq: Annotated[
        int | None,
        typer.Option("--through-seq", help="Review seq 1 to this one (default: every event)."),
    ] = None,
) -> None:
    """One audit review in an OpenShell sandbox: an AuditReviewRequested event, the audit
    extract under var/audit/<seq>/audit, and the reviewer's findings."""
    settings = Settings()
    with Container.build(settings) as c:
        log = _configure_logging(c.clock)
        try:
            report = c.agent_runner(log).run_audit_review(c.write_audit_extract, through_seq)
        except (SandboxRequiredError, ValueError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(
            f"completed {len(report.completed)} review run(s), failed {len(report.failed)}, "
            f"findings appended {len(report.findings_appended)}, skipped as duplicates "
            f"{len(report.findings_skipped)}"
        )
        for agent_id, seq in report.completed:
            typer.echo(f"  done   {agent_id} on AuditReviewRequested seq {seq}")
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on AuditReviewRequested seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)


from meridian.cli_substrate import substrate_app  # noqa: E402 (registered last: it imports cli lazily)

app.add_typer(substrate_app, name="substrate")

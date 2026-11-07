"""Command line interface: `meridian ledger ...`."""

import sys
from collections.abc import MutableMapping
from typing import Annotated, Any

import httpx
import polars as pl
import structlog
import typer

from meridian.data.cut_writer import CutExistsError
from meridian.data.fetch import FetchError, fetch
from meridian.data.ingest import IngestError
from meridian.data.sites import SiteMappingError, load_site_map
from meridian.data.study_store import StudyAccessError, StudyReader
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import DataCut, EventDraft, FindingRaised
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.store import LedgerError
from meridian.ledger.verify import verify
from meridian.platform.clock import Clock
from meridian.platform.config import Settings
from meridian.platform.container import Container

app = typer.Typer(no_args_is_help=True, help="Meridian Agent Fleet.")
ledger_app = typer.Typer(no_args_is_help=True, help="Inspect and manage the event ledger.")
data_app = typer.Typer(no_args_is_help=True, help="Fetch, ingest and inspect study data.")
app.add_typer(ledger_app, name="ledger")
app.add_typer(data_app, name="data")

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
        return frame.group_by("SITEID").agg(pl.len().alias(name))

    ae = per_site(reader.table("ae").join(dm, on="USUBJID", how="left"), "ae_count")
    vs = per_site(reader.table("vs").join(dm, on="USUBJID", how="left"), "vs_readings")
    queries = per_site(reader.table("queries").filter(pl.col("status") == "open"), "open_queries")
    deviations = per_site(reader.table("deviations"), "deviations")
    screening = (
        reader.table("screening")
        .group_by("SITEID")
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

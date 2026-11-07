"""Command line interface: `meridian ledger ...`."""

import sys
from collections.abc import MutableMapping
from typing import Annotated, Any

import structlog
import typer

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
app.add_typer(ledger_app, name="ledger")

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

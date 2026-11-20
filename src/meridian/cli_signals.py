"""meridian signals: portfolio.safety_signal_scout's scans of public FAERS data (phase 5
step 3a; docs/signal-scout.md)."""

import datetime as dt
from typing import Annotated

import typer

from meridian.platform.config import Settings
from meridian.platform.container import Container

signals_app = typer.Typer(
    no_args_is_help=True,
    help="Signals of disproportionate reporting in openFDA for MRD-201's drug.",
)


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


@signals_app.command("scan")
def signals_scan(
    by: Annotated[
        str | None,
        typer.Option("--by", help="The operator id recorded (operator:<name>); never a person."),
    ] = None,
    reuse: Annotated[
        int | None,
        typer.Option(
            "--reuse",
            help="Rerun on this earlier scan's recorded responses; ask only what it lacks.",
        ),
    ] = None,
) -> None:
    """Record a SignalScanRequested and run the scout on it in its OpenShell sandbox
    (portfolio_signal: egress to api.fda.gov only). Each response is kept under
    [signal_root] and named on the run's ActorWoke."""
    from meridian.domain.enums import Visibility
    from meridian.domain.events import EventDraft, SignalScanRequested
    from meridian.domain.identity import (
        NotAnOperatorError,
        operator_id,
        people_ids,
        require_operator,
    )

    settings = Settings()
    cfg = settings.agents.safety_signal_scout
    try:
        operator = require_operator(by or operator_id(), people_ids(settings.api.people_file))
    except NotAnOperatorError as exc:
        raise _fail(str(exc)) from exc
    with Container.build(settings) as c:
        from meridian.cli import _configure_logging

        log = _configure_logging(c.clock)
        if reuse is not None:
            from meridian.domain.events import Event
            from meridian.portfolio.store import kept

            base = next((e for e in c.ledger.read() if e.seq == reuse), None)
            if not isinstance(base, Event) or not isinstance(base.payload, SignalScanRequested):
                raise _fail(f"seq {reuse} is not a signal scan")
            if kept(settings.signal_root, reuse) is None:
                raise _fail(f"no recorded responses for scan seq {reuse}")
        now = c.clock.now()
        scan_id = "SCAN-" + now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        event = c.ledger.append(
            EventDraft(
                occurred_at=now,
                actor=operator,
                study_id=cfg.study_id,
                visibility=Visibility.BLINDED,
                payload=SignalScanRequested(
                    study_id=cfg.study_id,
                    scan_id=scan_id,
                    requested_by=operator,
                    reuses_responses_of=reuse,
                ),
            )
        )
        typer.echo(
            f"SignalScanRequested {scan_id} at ledger seq {event.seq} by {operator}"
            + (f", reusing the responses of seq {reuse}" if reuse is not None else "")
        )
        report = c.agent_runner(log).run_pending(
            agents={"portfolio.safety_signal_scout"}, cuts={scan_id}
        )
        typer.echo(
            f"completed {len(report.completed)}, failed {len(report.failed)}, findings "
            f"appended {len(report.findings_appended)}"
        )
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)


@signals_app.command("replay")
def signals_replay(
    trigger_seq: Annotated[int, typer.Option("--trigger", help="The scan's ledger seq.")],
) -> None:
    """Run the scout again, in process, on the responses kept for a scan; nothing is
    fetched and nothing recorded. Compares the drafts' sha256 with the recorded run's."""
    from meridian.agents.runner import drafts_sha256
    from meridian.domain.enums import EventType
    from meridian.domain.events import ActorSuspended, Event, SignalScanRequested
    from meridian.sandbox.executor import ExecutionError

    settings = Settings()
    with Container.build(settings) as c:
        from meridian.cli import _configure_logging

        log = _configure_logging(c.clock)
        events = {e.seq: e for e in c.ledger.read() if isinstance(e, Event)}
        trigger = events.get(trigger_seq)
        if trigger is None or not isinstance(trigger.payload, SignalScanRequested):
            raise _fail(f"seq {trigger_seq} is not a signal scan")
        recorded = [
            e.payload.drafts_sha256
            for e in c.ledger.read(event_types=[EventType.ACTOR_SUSPENDED])
            if isinstance(e, Event)
            and isinstance(e.payload, ActorSuspended)
            and e.payload.trigger_seq == trigger_seq
        ]
        try:
            result = c.inprocess_executor(log).run(
                "portfolio.safety_signal_scout", trigger, trigger.payload.scan_id
            )
        except ExecutionError as exc:
            raise _fail(str(exc)) from exc
        mine = drafts_sha256(result.drafts)
        same = bool(recorded) and recorded[-1] == mine
        typer.echo(
            f"replay of {trigger.payload.scan_id}: {len(result.drafts)} drafts, sha256 "
            f"{mine[:16]}; recorded {recorded[-1][:16] if recorded and recorded[-1] else 'none'}: "
            + ("identical" if same else "DIFFERENT")
        )
        if not same:
            raise typer.Exit(code=1)

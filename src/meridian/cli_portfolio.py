"""meridian portfolio: the monthly portfolio review (phase 5 step 3b; docs/stage-gate-notes.md).
People ask for reviews and gates through the Fleet API; this records only the scheduled
review, under the operator's identity, never a person's."""

import datetime as dt

import typer

from meridian.platform.config import Settings
from meridian.platform.container import Container

portfolio_app = typer.Typer(no_args_is_help=True, help="The portfolio's scheduled reviews.")
SCHEDULE = "system.schedule"


@portfolio_app.command("review-due")
def review_due() -> None:
    """Record this month's scheduled PortfolioReviewRequested (requested_by system.schedule)
    unless one exists for the month; then run portfolio.pipeline_intelligence on it.
    Meant for a monthly cron line the operator installs; this project installs none."""
    from meridian.domain.enums import EventType, Visibility
    from meridian.domain.events import REVIEW_PREFIX, Event, EventDraft, PortfolioReviewRequested
    from meridian.domain.identity import operator_id

    settings = Settings()
    with Container.build(settings) as c:
        from meridian.cli import _configure_logging

        log = _configure_logging(c.clock)
        now = c.clock.now().astimezone(dt.UTC)
        period = now.strftime("%Y-%m")
        done = [
            e
            for e in c.ledger.read(event_types=[EventType.PORTFOLIO_REVIEW_REQUESTED])
            if isinstance(e, Event)
            and isinstance(e.payload, PortfolioReviewRequested)
            and e.payload.reason == "scheduled"
            and e.payload.period == period
        ]
        if done:
            typer.echo(f"the {period} review is already recorded (seq {done[0].seq})")
        else:
            review_id = REVIEW_PREFIX + now.strftime("%Y%m%dT%H%M%SZ")
            event = c.ledger.append(
                EventDraft(
                    occurred_at=now,
                    actor=operator_id(),
                    visibility=Visibility.BLINDED,
                    payload=PortfolioReviewRequested(
                        review_id=review_id,
                        requested_by=SCHEDULE,
                        reason="scheduled",
                        period=period,
                    ),
                )
            )
            typer.echo(f"PortfolioReviewRequested {review_id} ({period}) at ledger seq {event.seq}")
        report = c.agent_runner(log).run_pending(agents={"portfolio.pipeline_intelligence"})
        typer.echo(f"completed {len(report.completed)}, failed {len(report.failed)}")
        for agent_id, seq, error in report.failed:
            typer.echo(f"  FAILED {agent_id} on seq {seq}: {error}")
        if report.failed:
            raise typer.Exit(code=1)

"""meridian demo: the narrated demo's host-side pipeline (docs/demo/README.md). It reads the
real ledger only to copy it; recording runs against the copy, through the read-only API."""

import json
from pathlib import Path
from typing import Annotated

import typer

from meridian.platform.config import Settings

demo_app = typer.Typer(no_args_is_help=True, help="The narrated demo (docs/demo/README.md).")


@demo_app.command("prepare")
def demo_prepare(
    piper: Annotated[str, typer.Option(help="The piper executable (a uv tool).")] = "piper",
) -> None:
    """Copy and verify the ledger (var/demo/ledger.sqlite3), synthesise the narration, and
    write the plan, captions, transcript, stats and card pages under var/demo/."""
    from meridian.demo import pipeline

    settings = Settings()
    try:
        plan = pipeline.prepare(
            settings.ledger_path, piper, settings.api.people_file, settings.cuts.operational
        )
    except pipeline.DemoError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    copy = plan["ledger_copy"]
    typer.echo(
        f"ledger copy: {copy['events']} events, verified, last hash {copy['last_hash'][:16]} "
        f"equals the source's at seq {copy['last_seq']}"
    )
    for s in plan["scenes"]:
        typer.echo(f"  scene {s['number']:2d} {s['duration']:6.1f} s  {s['title']}")
    total = plan["duration"]
    typer.echo(f"total {total:.1f} s ({int(total // 60)} min {total % 60:.0f} s)")
    if total > 480:
        typer.echo("over 8 minutes: cut scene 6, then scene 3 (docs/demo/script.md)", err=True)


@demo_app.command("assemble")
def demo_assemble(ffmpeg: Annotated[str, typer.Option()] = "ffmpeg") -> None:
    """Join the recorder's frames and the narration into var/demo/demo.mp4, with chapters."""
    from meridian.demo import pipeline

    video = pipeline.assemble(ffmpeg)
    plan = json.loads(Path("var/demo/plan.json").read_text(encoding="utf-8"))
    typer.echo(f"wrote {video} ({plan['duration']:.1f} s)")

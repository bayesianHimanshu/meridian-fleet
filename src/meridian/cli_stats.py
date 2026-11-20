"""meridian stats (wrap-up): the project's numbers from the ledger (meridian.project_stats).
Read only."""

import json
import tomllib
from pathlib import Path
from typing import Annotated

import typer

from meridian.platform.config import Settings
from meridian.platform.container import Container

JSON_FILE = "project-stats.json"
MARKDOWN_FILE = "project-stats.md"


def write_stats(out_dir: Path | None, markdown_only: bool = False) -> dict[str, object]:
    from meridian import project_stats

    settings = Settings()
    if not settings.ledger_path.exists():
        typer.echo(f"no ledger at {settings.ledger_path}: run `meridian ledger init`", err=True)
        raise typer.Exit(code=1)
    with settings.api.people_file.open("rb") as handle:
        people = {pid: p["role"] for pid, p in tomllib.load(handle)["people"].items()}
    with Container.build(settings) as c:
        stats = project_stats.compute(
            c.ledger,
            operational_cuts=settings.cuts.operational,
            people=people,
            var_dir=settings.ledger_path.parent,
        )
    text = json.dumps(stats, indent=2, ensure_ascii=False) + "\n"
    page = project_stats.markdown(stats)
    if out_dir is None:
        typer.echo(page if markdown_only else text, nl=False)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / JSON_FILE).write_text(text, encoding="utf-8")
        (out_dir / MARKDOWN_FILE).write_text(page, encoding="utf-8")
        typer.echo(
            f"wrote {out_dir / JSON_FILE} and {out_dir / MARKDOWN_FILE} "
            f"(ledger seq {stats['computed_at_seq']})"
        )
    if not stats["verification"]["ok"]:
        typer.echo("the ledger does not verify", err=True)
        raise typer.Exit(code=1)
    return stats


def stats(
    markdown: Annotated[bool, typer.Option("--markdown", help="Markdown instead of JSON.")] = False,
    write: Annotated[
        Path | None,
        typer.Option("--write", help=f"Write {JSON_FILE} and {MARKDOWN_FILE} into this folder."),
    ] = None,
) -> None:
    """The project's numbers from the ledger: events by type; findings by zone, agent and
    status; human decisions by role and action; runs by executor and cut scope; policy
    refusals; leak checks (result files); agents and versions; the ledger's verification.
    Exit 1 if the ledger does not verify."""
    write_stats(write, markdown)

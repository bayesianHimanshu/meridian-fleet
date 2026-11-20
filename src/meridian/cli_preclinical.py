"""meridian preclinical: MRD-101's SEND study and preclinical.tox_study_monitor."""

import hashlib
import json
from typing import Annotated

import httpx
import typer

from meridian.data.cut_writer import CutExistsError
from meridian.data.fetch import FetchError, fetch
from meridian.data.send import SendIngestError, check_pinned, ingest_send
from meridian.platform.config import Settings
from meridian.platform.container import Container

preclinical_app = typer.Typer(
    no_args_is_help=True,
    help="MRD-101's SEND study (docs/preclinical-data.md) and its toxicology agent.",
)


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(code=1)


@preclinical_app.command("fetch")
def preclinical_fetch() -> None:
    """Download the pinned SEND files once and check each against its pinned sha256."""
    with Container.build(Settings()) as c:
        p = c.settings.preclinical
        try:
            with httpx.Client(
                follow_redirects=True, timeout=p.timeout_seconds, trust_env=True
            ) as client:
                report = fetch(
                    client=client,
                    clock=c.clock,
                    base_url=p.base_url,
                    domains=sorted(p.files),
                    raw_dir=p.raw_dir,
                )
            check_pinned(p.raw_dir, p.files)
        except (FetchError, SendIngestError, httpx.HTTPError, OSError) as exc:
            raise _fail(exc) from exc
        for name, entry in sorted(report.manifest.files.items()):
            state = "downloaded" if name in report.downloaded else "unchanged"
            typer.echo(f"{name:<8} {entry.size_bytes:>9} bytes  sha256 {entry.sha256}  {state}")
        typer.echo(f"all {len(p.files)} files match their pinned sha256")


@preclinical_app.command("ingest")
def preclinical_ingest() -> None:
    """Curate the fetched study into <send_root>/<study>/<cut> and record SendStudyLoaded."""
    with Container.build(Settings()) as c:
        p = c.settings.preclinical
        try:
            result = ingest_send(
                raw_dir=p.raw_dir,
                pinned=p.files,
                send_root=p.send_root,
                study_id=p.study_id,
                cut_id=p.cut_id,
                source_study_id=p.source_study_id,
                treatment=p.treatment,
                source_treatment=p.source_treatment,
                ledger=c.ledger,
                occurred_at=c.clock.now(),
            )
        except (SendIngestError, CutExistsError, OSError) as exc:
            raise _fail(exc) from exc
        state = "written" if result.cut.created else "already present, identical"
        typer.echo(f"SEND cut {p.study_id}/{p.cut_id} {state} at {result.cut.path}")
        for f in result.cut.manifest.files:
            typer.echo(f"  {f.path:<20} {f.rows:>6} rows  sha256 {f.sha256[:16]}")
        verb = "appended" if result.event_appended else "already recorded"
        typer.echo(f"SendStudyLoaded {verb} at ledger seq {result.event.seq}")


@preclinical_app.command("eval")
def preclinical_eval(
    which: Annotated[str, typer.Option("--set", help="null, dev or holdout")],
) -> None:
    """Run the agent in process on the pre-registered evaluation copies (no ledger
    writes) and write data/evals/preclinical/<set>.json; ground truth for dev and holdout
    goes to data/ground_truth/preclinical/ (docs/preclinical-eval.md)."""
    from meridian.data.cut_writer import CutManifest
    from meridian.evals.preclinical import (
        dumps,
        evaluate_null,
        evaluate_set,
        load_study,
        summarise,
    )
    from meridian.ledger.hashing import canonical_json
    from meridian.preclinical.inprocess import run_checks

    if which not in ("null", "dev", "holdout"):
        raise _fail(ValueError(f"--set must be null, dev or holdout, not {which}"))
    s = Settings()
    p, cfg = s.preclinical, s.agents.tox_study_monitor
    cut_dir = p.send_root / p.study_id / p.cut_id
    base = load_study(cut_dir)
    manifest = CutManifest.load(cut_dir)
    base_sha = hashlib.sha256(canonical_json(manifest.model_dump(mode="json")).encode()).hexdigest()
    run = run_checks(cfg, p.study_id)
    if which == "null":
        hem = cfg.magnitude_pct.get("hematology", 10.0)
        plain = run_checks(cfg.model_copy(update={"red_cell_magnitude_pct": hem}), p.study_id)
        results = evaluate_null(base, run, plain)
    else:
        results = evaluate_set(which, base, cfg, run, base_sha, s.ground_truth_dir / "preclinical")
    out = s.evals_dir / "preclinical" / f"{which}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        dumps(
            results,
            {
                "set": which,
                "config": cfg.model_dump(mode="json"),
                "base_cut": p.cut_id,
                "base_manifest_sha256": base_sha,
            },
        )
    )
    typer.echo(json.dumps(summarise(results), indent=2, sort_keys=True))
    typer.echo(f"wrote {out}")


@preclinical_app.command("inventory-load")
def preclinical_inventory_load() -> None:
    """Write the committed nonclinical inventory (data/inventory/<asset>/inventory.csv) as
    a cut under [preclinical] inventory_root and record InventoryLoaded once per cut: the
    trigger of preclinical.ind_readiness (docs/ind-readiness-eval.md)."""
    from meridian.preclinical.inputs import record_inventory, write_inventory

    with Container.build(Settings()) as c:
        p = c.settings.preclinical
        try:
            cut, path, sha = write_inventory(p.inventory_file, p.inventory_root, p.asset_id)
        except (OSError, ValueError) as exc:
            raise _fail(exc) from exc
        event, appended = record_inventory(
            c.ledger,
            c.ledger,
            occurred_at=c.clock.now(),
            actor="system.ingest",
            asset_id=p.asset_id,
            cut_id=cut,
            path=path,
            source_sha256=sha,
        )
        typer.echo(f"inventory {p.asset_id}/{cut} at {path} (csv sha256 {sha[:16]})")
        verb = "appended" if appended else "already recorded"
        typer.echo(f"InventoryLoaded {verb} at ledger seq {event.seq}")

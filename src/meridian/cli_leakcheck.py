"""`meridian leakcheck --env kind|gke`: the canary leak check (meridian.sandbox.leakcheck).

On GKE run it with MERIDIAN_CONFIG_OVERLAY=deploy/gke/meridian.toml (deploy/gke/run.sh
sets it). The decided order (docs/gke-session.md): first --zone rbqm --zone quality
--zone quality.audit_trail; the safety zone moves only after that passes, and then the
check runs again with it included.
"""

import datetime as dt
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.substrate import SubstrateError

DISPATCHER_SELECTOR = "workload=meridian-dispatcher"
ZONE_AGENTS = {
    "rbqm": ("rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm"),
    "safety.sae_reconciler": ("safety.sae_reconciler",),
    "quality": ("quality.tmf_completeness",),
    "quality.audit_trail": ("quality.audit_trail_reviewer",),
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def leakcheck(
    env: Annotated[str, typer.Option("--env", help="kind or gke; must match [substrate] target.")],
    zones: Annotated[
        list[str] | None,
        typer.Option("--zone", help="A substrate policy to run; repeatable. Default: all four."),
    ] = None,
    runs: Annotated[int, typer.Option("--runs", min=1)] = 20,
    cut_id: Annotated[str, typer.Option("--cut")] = "DC-BASE",
    node_scan: Annotated[bool, typer.Option("--node-scan/--no-node-scan")] = True,
    out: Annotated[Path | None, typer.Option("--out")] = None,
) -> None:
    """Plant a canary in copies of the inputs, run the zones through the dispatcher, and
    search every reachable store for it; each store needs its positive control found."""
    from meridian.domain.enums import EventType
    from meridian.domain.events import AuditReviewRequested, Event
    from meridian.sandbox import gke
    from meridian.sandbox import leakcheck as lc
    from meridian.sandbox import substrate_proofs as sp
    from meridian.sandbox.substrate_executor import load_template
    from meridian.sandbox.substrate_templates import CONFORMANCE, SUBSTRATE_POLICIES
    from meridian.sandbox.verify import trigger_for

    base = Settings()
    s = base.substrate
    if env != s.target:
        typer.echo(
            f"error: --env {env} but [substrate] target is {s.target}"
            + (" (set MERIDIAN_CONFIG_OVERLAY=deploy/gke/meridian.toml)" if env == "gke" else ""),
            err=True,
        )
        raise typer.Exit(code=2)
    chosen = list(zones or SUBSTRATE_POLICIES)
    unknown = sorted(set(chosen) - set(ZONE_AGENTS))
    if unknown:
        typer.echo(f"error: no such substrate zone: {unknown}", err=True)
        raise typer.Exit(code=2)
    agents = [a for z in chosen for a in ZONE_AGENTS[z]]
    canary, marker = sp.new_canary(), lc.new_marker()
    needles = [canary.encode(), marker.encode(), lc.WORKER_MARKER.encode()]
    result = lc.LeakCheck(
        env=env, zones=chosen, canary_sha256=hashlib.sha256(needles[0]).hexdigest()
    )
    start = _now()
    result.started = start.isoformat()
    with tempfile.TemporaryDirectory(prefix="meridian-leakcheck-") as tmp:
        with Container.build(base) as c0:
            audit = [
                e
                for e in c0.ledger.read(event_types=[EventType.AUDIT_REVIEW_REQUESTED])
                if isinstance(e, Event) and isinstance(e.payload, AuditReviewRequested)
            ]
        audit_trigger = audit[-1] if audit else None
        roots = sp.make_copies(
            canary=canary,
            dest=Path(tmp),
            study_id=base.study_id,
            cut_id=cut_id,
            curated_root=base.curated_root,
            safety_root=base.safety_root,
            etmf_root=base.etmf_root,
            audit_root=base.audit_root,
            audit_seq=None if audit_trigger is None else audit_trigger.seq,
        )
        settings = base.model_copy(
            update={
                "curated_root": roots.curated_root,
                "safety_root": roots.safety_root,
                "etmf_root": roots.etmf_root,
                "audit_root": roots.audit_root,
            }
        )
        with Container.build(settings) as c:
            from meridian.cli import _configure_logging

            log = _configure_logging(c.clock)
            try:
                executor = c.substrate_executor(log, chosen)
                client = c.dispatcher_client()
                probe = load_template(client, s.deploy_dir, CONFORMANCE, probe=True)
            except (SubstrateError, ExecutionError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            golden = {executor.template_for_policy(p).golden_snapshot for p in chosen} | {
                load_template(client, s.deploy_dir, p).golden_snapshot for p in SUBSTRATE_POLICIES
            }
            golden.add(probe.golden_snapshot)
            trigger = trigger_for(c.ledger, c.settings.study_id, cut_id)
            for i in range(runs):
                agent = agents[i % len(agents)]
                t, cut = trigger, cut_id
                if agent == "quality.audit_trail_reviewer":
                    if audit_trigger is None:
                        continue
                    t, cut = audit_trigger, "LEDGER"
                try:
                    r = executor.run(agent, t, cut)
                except ExecutionError as exc:
                    typer.echo(f"error: run {i + 1} ({agent}): {exc}", err=True)
                    raise typer.Exit(code=1) from exc
                assert r.substrate is not None
                result.runs.append(
                    {"agent": agent, "actor": r.substrate.actor, "ms": r.duration_ms}
                )
                typer.echo(f"run {i + 1:>2} {agent:<30} {r.substrate.actor} {r.duration_ms} ms")
            executor.drain()
            # The marker into the dispatcher's log (a request path) and its /tmp.
            client.lookup(marker)
            kubectl, context, ns = s.kubectl, s.context, s.dispatcher_namespace
            pod = lc.pod_name(kubectl, context, ns, DISPATCHER_SELECTOR)
            lc.write_in_pod(kubectl, context, ns, pod, lc.POD_CONTROL_PATH, marker)
            try:
                typer.echo(f"searching the dispatcher pod {pod} and its logs ...")
                result.sources.append(
                    lc._marker_source(
                        "dispatcher pod",
                        lc.search_pod_tree(kubectl, context, ns, pod, needles),
                        f"the marker in {lc.POD_CONTROL_PATH}",
                    )
                )
            finally:
                lc.write_in_pod(kubectl, context, ns, pod, lc.POD_CONTROL_PATH, None)
            result.sources.append(
                lc._marker_source(
                    "dispatcher logs",
                    lc.search_pod_logs(kubectl, context, ns, pod, needles),
                    "the marker, logged as a request path",
                )
            )
            typer.echo("searching the snapshot bucket ...")
            gcloud = gke.run_gcloud(s.gke.gcloud)
            if s.target == "kind":
                bucket, keys = lc.search_rustfs(kubectl, context, needles)
            else:
                bucket, keys = lc.search_gcs(gcloud, s.gke.bucket, needles)
            result.sources.append(lc.golden_control(bucket, keys, s.atespace, golden))
            result.layout = sp.bucket_layout(keys, s.atespace, golden)
            if node_scan:
                image = executor_image(c)
                with lc.scan_namespace(kubectl, context):
                    for node in lc.nodes(kubectl, context):
                        typer.echo(f"searching node {node} from a scan pod ...")
                        result.sources.append(
                            lc.search_node(kubectl, context, node, image, canary, marker)
                        )
            typer.echo("searching the host's var/ ...")
            result.sources.append(lc.search_host_dir(Path("var"), needles, marker))
            if s.target == "gke":
                typer.echo("searching Cloud Logging over the run window ...")
                result.sources.append(
                    lc.search_cloud_logging(
                        gcloud,
                        s.gke.project_id,
                        start - dt.timedelta(minutes=1),
                        _now() + dt.timedelta(minutes=1),
                        marker,
                        needles,
                    )
                )
    result.finished = _now().isoformat()
    del canary, needles
    path = out or Path("var/leakcheck") / f"{env}-{start.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    typer.echo("| Source | Items | Bytes | Canary | Positive control |")
    typer.echo("| --- | ---: | ---: | --- | --- |")
    for src in result.sources:
        typer.echo(
            f"| {src.name} | {src.report.items} | {src.report.bytes_scanned} | "
            f"{'FOUND' if src.canary_hits else 'none'} | "
            f"{'found' if src.control_found else 'MISSING'}: {src.control} |"
        )
    typer.echo(f"bucket layout: {json.dumps(result.layout)[:400]}")
    if not result.passed:
        for failure in result.failures():
            typer.echo(f"FAIL {failure}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"pass: {len(result.runs)} runs ({', '.join(chosen)}); wrote {path}")


def executor_image(c: Container) -> str:
    """The scan pods' image: the dispatcher's (it holds meridian.sandbox.leakscan and is in
    the registry the nodes pull from), by digest."""
    from meridian.sandbox.dispatcher_deploy import MANIFEST, committed_image

    return committed_image(c.settings.substrate.dispatcher_deploy_dir / MANIFEST)

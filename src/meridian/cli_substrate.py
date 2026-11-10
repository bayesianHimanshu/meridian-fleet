"""`meridian substrate ...`: Agent Substrate for the zones without network (phase 4a).

render, deploy and status manage what this project owns in its atespace; verify,
conformance, canary and bench are the proofs and measurements of docs/substrate-eval.md.
"""

from pathlib import Path
from typing import Annotated

import typer

from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.substrate import SubstrateError

substrate_app = typer.Typer(
    no_args_is_help=True, help="Run no-network zones in Agent Substrate actors (kind cluster)."
)

ImageOption = Annotated[
    str | None,
    typer.Option("--image", help="Registry reference by digest; default: the committed one."),
]


def _log(c: Container):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from meridian.cli import _configure_logging

    return _configure_logging(c.clock)


def _fail(exc: Exception, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(code=code)


def write_rendered(settings: Settings, image_ref: str) -> list[Path]:
    from meridian.sandbox.substrate_templates import (
        CONFORMANCE,
        POOL_FILE,
        SUBSTRATE_POLICIES,
        PoolSpec,
        render_pool,
        render_template,
        template_path,
    )

    s = settings.substrate
    root = s.deploy_dir
    written = []
    for policy in (*SUBSTRATE_POLICIES, CONFORMANCE):
        path = template_path(root, policy)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_template(policy, s.atespace, image_ref), encoding="utf-8")
        written.append(path)
    pool = PoolSpec(
        namespace=s.namespace,
        name=s.worker_pool,
        replicas=s.workers,
        worker_image=s.worker_image,
        sandbox_class=s.sandbox_class,
    )
    (root / POOL_FILE).write_text(render_pool(pool), encoding="utf-8")
    written.append(root / POOL_FILE)
    return written


def _committed_image(settings: Settings) -> str:
    from meridian.sandbox.substrate_templates import committed_image, template_path

    return committed_image(template_path(settings.substrate.deploy_dir, "rbqm"))


@substrate_app.command("render")
def substrate_render(image_ref: ImageOption = None) -> None:
    """Write deploy/substrate/: the WorkerPool and one template per zone, plus conformance."""
    settings = Settings()
    ref = image_ref or _committed_image(settings)
    for path in write_rendered(settings, ref):
        typer.echo(f"wrote {path}")


@substrate_app.command("deploy")
def substrate_deploy(
    prune: Annotated[
        bool, typer.Option("--prune", help="Delete templates the committed files no longer name.")
    ] = False,
) -> None:
    """Push the agent image to the kind registry, render deploy/substrate for its digest,
    apply the WorkerPool, create the atespace and templates, and wait for golden snapshots."""
    from meridian.sandbox import substrate_deploy as d
    from meridian.sandbox.substrate_templates import (
        CONFORMANCE,
        POOL_FILE,
        SUBSTRATE_POLICIES,
        template_path,
    )

    settings = Settings()
    s = settings.substrate
    with Container.build(settings) as c:
        try:
            c.check_substrate_release()
            pushed = d.push_image(c.image_ref(), s.registry)
            typer.echo(f"pushed {pushed.ref} (image id {pushed.config_digest})")
            write_rendered(settings, pushed.ref)
            d.apply_pool(s.kubectl, s.context, s.deploy_dir / POOL_FILE)
            d.wait_pool(s.kubectl, s.context, s.namespace, s.worker_pool, timeout=300)
            client = c.substrate_client()
            d.ensure_atespace(client, s.kubectl, s.context)
            names = [
                d.create_template(client, s.kubectl, s.context, template_path(s.deploy_dir, p))
                for p in (*SUBSTRATE_POLICIES, CONFORMANCE)
            ]
            for name, seconds in d.wait_golden(client, names, timeout=600).items():
                typer.echo(f"golden snapshot of {name} after {seconds} s")
            if prune:
                for name in d.prune_templates(client, s.kubectl, s.context, names):
                    typer.echo(f"deleted template {name}")
        except (d.DeployError, SubstrateError, ExecutionError, OSError) as exc:
            raise _fail(exc) from exc


@substrate_app.command("status")
def substrate_status() -> None:
    """The templates the committed files name, their golden snapshots, and live actors."""
    from meridian.sandbox.substrate_executor import load_template
    from meridian.sandbox.substrate_templates import CONFORMANCE, SUBSTRATE_POLICIES

    with Container.build(Settings()) as c:
        client = c.substrate_client()
        try:
            c.check_substrate_release()
            for policy in (*SUBSTRATE_POLICIES, CONFORMANCE):
                t = load_template(
                    client, c.settings.substrate.deploy_dir, policy, probe=policy == CONFORMANCE
                )
                typer.echo(
                    f"{policy:<22} {t.name:<36} golden {t.golden_snapshot} spec {t.sha256[:12]}"
                )
            actors = client.actors()
        except (SubstrateError, ExecutionError) as exc:
            raise _fail(exc) from exc
        typer.echo(f"live actors in {client.atespace}: {len(actors)}")
        for a in actors:
            meta, status = a.get("metadata", {}), a.get("status", {})
            typer.echo(f"  {meta.get('name')} {status.get('state')}")


@substrate_app.command("verify")
def substrate_verify(
    cuts: Annotated[list[str] | None, typer.Option("--cut", help="Repeatable.")] = None,
    agents: Annotated[list[str] | None, typer.Option("--agent", help="Repeatable.")] = None,
) -> None:
    """Run agents in OpenShell and in Substrate; compare drafts byte for byte. Defaults:
    the 12 pairs of phase 2 step 1 (3 rbqm agents on 4 cuts)."""
    from meridian.sandbox.verify import verify

    cut_ids = cuts or ["DC-BASE", "DC-RBQM-CORE", "DC-NULL-S6", "DC-d5ada06c"]
    agent_ids = agents or ["rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm"]
    with Container.build(Settings()) as c:
        log = _log(c)
        try:
            rows = verify(
                ledger=c.ledger,
                study_id=c.settings.study_id,
                registry=c.agent_registry(),
                inprocess=c.openshell_executor(log),
                sandboxed=c.substrate_executor(log),
                cuts=cut_ids,
                agents=agent_ids,
            )
        except (ExecutionError, SubstrateError, LookupError, OSError) as exc:
            raise _fail(exc) from exc
    typer.echo("| Agent | Cut | Identical | Drafts | Substrate ms | OpenShell ms |")
    typer.echo("| --- | --- | --- | ---: | ---: | ---: |")
    for r in rows:
        typer.echo(
            f"| {r.agent_id} | {r.cut_id} | {'yes' if r.identical else 'NO'} | {r.drafts} | "
            f"{r.sandbox_ms} | {r.inprocess_ms} |"
        )
    if not all(r.identical for r in rows):
        raise typer.Exit(code=1)


@substrate_app.command("reap")
def substrate_reap() -> None:
    """Delete every actor in var/actors.jsonl not yet deleted, and only those."""
    from meridian.sandbox.substrate import NotFoundError
    from meridian.sandbox.tracking import SandboxTracker

    with Container.build(Settings()) as c:
        tracker = SandboxTracker(c.settings.substrate.actors_log, c.clock)
        client = c.substrate_client()
        for actor in tracker.pending():
            try:
                client.delete_actor(actor)
            except NotFoundError:
                pass
            except SubstrateError as exc:
                raise _fail(exc) from exc
            tracker.record(actor, "deleted", "reaped")
            typer.echo(f"deleted {actor}")


@substrate_app.command("conformance")
def substrate_conformance(
    zones: Annotated[
        list[str] | None, typer.Option("--zone", help="Policy to check; repeatable. Default: all.")
    ] = None,
    record: Annotated[
        bool, typer.Option("--record/--no-record", help="Append the events to the ledger.")
    ] = True,
) -> None:
    """Containment checks in real actors for each substrate policy; ledger events as for
    OpenShell (PolicyDenied, ContainmentCheckCompleted with executor substrate)."""
    from meridian.sandbox import substrate_conformance as sc
    from meridian.sandbox.substrate_deploy import registry_config_digest
    from meridian.sandbox.substrate_executor import load_template
    from meridian.sandbox.substrate_templates import CONFORMANCE

    with Container.build(Settings()) as c:
        log = _log(c)
        s = c.settings.substrate
        client = c.substrate_client()
        failed = False
        try:
            executor = c.substrate_executor(log, client)
            probe = load_template(client, s.deploy_dir, CONFORMANCE, probe=True)
            digest = registry_config_digest(probe.image)
            for zone in zones or c.substrate_policies():
                template = executor.template_for_policy(zone)
                result = sc.run_substrate_conformance(
                    client=client,
                    zone=zone,
                    zone_template=template,
                    probe_template=probe,
                    placement=executor.placement,
                    image_digest=digest,
                    kubectl=s.kubectl,
                    context=s.context,
                )
                verdict = "pass" if result.passed else "FAIL"
                typer.echo(f"{verdict}  {zone} (actor {result.actor}, peer {result.peer_ip})")
                for check in result.checks:
                    typer.echo(f"  {check.name:<20} {check.outcome:<12} {check.message[:110]}")
                typer.echo(f"  atenet-egress lines naming a target: {len(result.gateway_lines)}")
                for line in result.gateway_lines[:6]:
                    typer.echo(f"    {line[:200]}")
                if record:
                    for draft in sc.ledger_drafts(result, c.clock):
                        c.ledger.append(draft)
                failed = failed or not result.passed
        except (SubstrateError, ExecutionError) as exc:
            raise _fail(exc) from exc
        if failed:
            raise typer.Exit(code=1)


CANARY_AGENTS = (
    "rbqm.kri_monitor",
    "rbqm.qtl_detector",
    "rbqm.csm",
    "safety.sae_reconciler",
    "quality.tmf_completeness",
    "quality.audit_trail_reviewer",
)


@substrate_app.command("canary")
def substrate_canary(
    runs: Annotated[int, typer.Option("--runs", min=1)] = 20,
    cut_id: Annotated[str, typer.Option("--cut")] = "DC-BASE",
    positive_control: Annotated[
        bool,
        typer.Option(
            "--positive-control/--no-positive-control",
            help="Afterwards suspend one canary actor and show the search finds it.",
        ),
    ] = True,
    node_scan: Annotated[bool, typer.Option("--node-scan/--no-node-scan")] = True,
    out: Annotated[Path, typer.Option("--out")] = Path("var/substrate/canary.json"),
) -> None:
    """Proof C1: a canary in the staged inputs of N runs is found in no snapshot, worker
    filesystem or host folder afterwards (meridian.sandbox.substrate_proofs)."""
    import contextlib
    import hashlib
    import json
    import tempfile
    import time
    import uuid

    import httpx

    from meridian.domain.enums import EventType
    from meridian.domain.events import AuditReviewRequested, Event
    from meridian.sandbox import substrate_proofs as sp
    from meridian.sandbox.executor import stage_run
    from meridian.sandbox.substrate_executor import archive, load_template
    from meridian.sandbox.substrate_templates import CONFORMANCE
    from meridian.sandbox.verify import trigger_for
    from meridian.worker_serve import AGENT_HEADER, SHA_HEADER

    base = Settings()
    s = base.substrate
    canary = sp.new_canary()
    needle = canary.encode()
    report: dict[str, object] = {"canary_sha256": hashlib.sha256(needle).hexdigest()}
    with tempfile.TemporaryDirectory(prefix="meridian-canary-") as tmp:
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
        report["planted"] = list(roots.planted)
        settings = base.model_copy(
            update={
                "curated_root": roots.curated_root,
                "safety_root": roots.safety_root,
                "etmf_root": roots.etmf_root,
                "audit_root": roots.audit_root,
            }
        )
        with Container.build(settings) as c:
            log = _log(c)
            client = c.substrate_client()
            try:
                executor = c.substrate_executor(log, client)
                probe = load_template(client, s.deploy_dir, CONFORMANCE, probe=True)
                zone_templates = [executor.template_for_policy(p) for p in c.substrate_policies()]
            except (SubstrateError, ExecutionError) as exc:
                raise _fail(exc) from exc
            golden = {t.golden_snapshot for t in zone_templates} | {probe.golden_snapshot}
            trigger = trigger_for(c.ledger, c.settings.study_id, cut_id)
            done = []
            for i in range(runs):
                agent = CANARY_AGENTS[i % len(CANARY_AGENTS)]
                t = trigger
                cut = cut_id
                if agent == "quality.audit_trail_reviewer":
                    if audit_trigger is None:
                        continue
                    t, cut = audit_trigger, "LEDGER"
                try:
                    r = executor.run(agent, t, cut)
                except ExecutionError as exc:
                    raise _fail(exc) from exc
                assert r.substrate is not None
                done.append({"agent": agent, "actor": r.substrate.actor, "ms": r.duration_ms})
                typer.echo(f"run {i + 1:>2} {agent:<30} {r.substrate.actor} {r.duration_ms} ms")
            executor.drain()
            report["runs"] = done
            report["live_actors_after"] = len(client.actors())
            typer.echo("searching snapshot storage, worker filesystems and var/ ...")

            def search_all(label: str) -> dict[str, object]:
                with sp.port_forward(s.kubectl, s.context, "ate-system", "rustfs", 9000) as port:
                    s3 = sp.LocalS3(f"http://127.0.0.1:{port}")
                    bucket, keys = sp.search_bucket(s3, needle)
                stores = [bucket.as_dict()]
                if node_scan and label == "after_runs":
                    stores.append(sp.search_node(needle).as_dict())
                    stores.append(sp.search_dir(Path("var"), needle, "host var/").as_dict())
                return {
                    "stores": stores,
                    "layout": sp.bucket_layout(keys, s.atespace, golden),
                }

            report["after_runs"] = search_all("after_runs")
            if positive_control:
                template = executor.template_for_policy("rbqm")
                actor = f"mer-pc-{uuid.uuid4().hex[:8]}"
                with tempfile.TemporaryDirectory(prefix="meridian-stage-") as stage_tmp:
                    staged = stage_run(
                        agent_id="rbqm.kri_monitor",
                        trigger=trigger,
                        cut_id=cut_id,
                        policy_name="rbqm",
                        dest=Path(stage_tmp),
                        study_root=roots.curated_root / base.study_id,
                        safety_root=None,
                        etmf_root=None,
                        audit_root=None,
                    )
                    body = archive(staged.stage)
                try:
                    client.create_actor(actor, template.name)
                    client.resume_actor(actor)
                    with client.proxy(actor, 8080) as address:
                        httpx.post(
                            f"http://{address}/run",
                            content=body,
                            headers={
                                AGENT_HEADER: "rbqm.kri_monitor",
                                SHA_HEADER: hashlib.sha256(body).hexdigest(),
                            },
                            timeout=120,
                        ).raise_for_status()
                    client.suspend_actor(actor)
                    report["positive_control_suspended"] = search_all("suspended")
                finally:
                    with contextlib.suppress(SubstrateError):
                        client.delete_actor(actor)
                time.sleep(30)
                report["positive_control_after_delete"] = search_all("deleted")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    after = report["after_runs"]
    assert isinstance(after, dict)
    hits = [h for st in after["stores"] for h in st["hits"]]
    typer.echo(json.dumps({k: v for k, v in report.items() if k != "runs"}, indent=1)[:6000])
    if hits or after["layout"]["outside_tags"] or after["layout"]["non_golden_tags"]:
        typer.echo("FAIL: the canary or a non-golden snapshot was found", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"pass: canary in none of the stores after {len(done)} runs; wrote {out}")


@substrate_app.command("bench")
def substrate_bench(
    executor_name: Annotated[str, typer.Option("--executor")] = "substrate",
    runs: Annotated[int, typer.Option("--runs", min=1)] = 100,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1)] = 1,
    agent: Annotated[str, typer.Option("--agent")] = "rbqm.kri_monitor",
    cut_id: Annotated[str, typer.Option("--cut")] = "DC-BASE",
    workers: Annotated[
        int | None, typer.Option("--workers", help="Scale the WorkerPool first.")
    ] = None,
    label: Annotated[str, typer.Option("--label")] = "",
    out: Annotated[Path, typer.Option("--out")] = Path("var/substrate/bench.jsonl"),
) -> None:
    """Time N runs of one agent on one cut (K at a time); append a JSON line to --out."""
    import json
    import time

    from meridian.sandbox import substrate_bench as sb
    from meridian.sandbox.verify import trigger_for

    settings = Settings()
    s = settings.substrate
    if executor_name == "openshell":
        # Synchronous deletion: a run's time includes its teardown, as in the baseline.
        settings = settings.model_copy(
            update={"runtime": settings.runtime.model_copy(update={"delete_workers": 0})}
        )
    elif executor_name != "substrate":
        raise _fail(ValueError("--executor is substrate or openshell"), code=2)
    with Container.build(settings) as c:
        log = _log(c)
        try:
            if workers is not None and executor_name == "substrate":
                sb.scale_pool(s.kubectl, s.context, s.namespace, s.worker_pool, workers)
            executor = (
                c.substrate_executor(log)
                if executor_name == "substrate"
                else c.openshell_executor(log)
            )
        except (SubstrateError, ExecutionError, OSError) as exc:
            raise _fail(exc) from exc
        trigger = trigger_for(c.ledger, c.settings.study_id, cut_id)
        before = sb.host_memory()
        mem_before = (
            sb.worker_memory(s.kubectl, s.context, s.namespace, s.worker_pool)
            if executor_name == "substrate"
            else []
        )
        start = time.perf_counter()
        results = sb.run_many(lambda: executor.run(agent, trigger, cut_id), runs, concurrency)
        elapsed = round(time.perf_counter() - start, 1)
        executor.drain()
        after = sb.host_memory()
        mem_after = (
            sb.worker_memory(s.kubectl, s.context, s.namespace, s.worker_pool)
            if executor_name == "substrate"
            else []
        )
    line = {
        "label": label,
        "executor": executor_name,
        "agent": agent,
        "cut": cut_id,
        "concurrency": concurrency,
        "workers": workers,
        "elapsed_s": elapsed,
        "host_mem_available_before": before.get("MemAvailable"),
        "host_mem_available_after": after.get("MemAvailable"),
        "worker_memory_before": mem_before,
        "worker_memory_after": mem_after,
        **sb.report(results),
        "wall_ms_each": [r.wall_ms for r in results],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    shown = {k: v for k, v in line.items() if k not in ("wall_ms_each",)}
    typer.echo(json.dumps(shown, indent=1)[:4000])
    if line["failed"]:
        raise typer.Exit(code=1)

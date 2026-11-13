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


def access_principals(settings: Settings) -> list[str]:
    """Who is editor on the atespace: only the in-cluster dispatcher (phase 4b step 3; the
    laptop's runner identity of phase 4a is gone, on kind as on GKE)."""
    from meridian.sandbox.dispatcher_deploy import principal

    return [principal(settings.substrate)]


def write_rendered(settings: Settings, image_ref: str) -> list[Path]:
    from meridian.sandbox.substrate_templates import (
        ACCESS_POLICY_FILE,
        CONFORMANCE,
        POOL_FILE,
        SUBSTRATE_POLICIES,
        PoolSpec,
        check_sizes,
        render_access_policy,
        render_pool,
        render_template,
        template_path,
    )

    s = settings.substrate
    check_sizes(s.actor_memory_mib, s.worker_memory_mib)
    root = s.deploy_dir
    written = []
    for policy in (*SUBSTRATE_POLICIES, CONFORMANCE):
        path = template_path(root, policy)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = render_template(policy, s.atespace, image_ref, s.actor_memory_mib)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    pool = PoolSpec(
        namespace=s.namespace,
        name=s.worker_pool,
        replicas=s.workers,
        worker_image=s.worker_image,
        sandbox_class=s.sandbox_class,
        worker_memory_mib=s.worker_memory_mib,
        node_selector=s.node_selector,
        toleration=s.toleration,
    )
    (root / POOL_FILE).write_text(render_pool(pool), encoding="utf-8")
    written.append(root / POOL_FILE)
    (root / ACCESS_POLICY_FILE).write_text(
        render_access_policy(s.atespace, access_principals(settings)), encoding="utf-8"
    )
    written.append(root / ACCESS_POLICY_FILE)
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
    """kind: push the agent image to the kind registry. GKE: take the image you pushed to
    Artifact Registry, by digest. Then render [substrate] deploy_dir for that digest, apply
    the WorkerPool, create the atespace, its access policy and templates, and wait for
    golden snapshots."""
    from meridian.sandbox import image
    from meridian.sandbox import substrate_deploy as d
    from meridian.sandbox.image import ImageError
    from meridian.sandbox.substrate_templates import (
        ACCESS_POLICY_FILE,
        CONFORMANCE,
        POOL_FILE,
        SUBSTRATE_POLICIES,
        access_policy_spec,
        template_path,
    )

    settings = Settings()
    s = settings.substrate
    with Container.build(settings) as c:
        try:
            c.check_substrate_release()
            commit = d.require_clean_source(image.inspect(c.image_ref()))
            typer.echo(f"{c.image_ref()} was built from {commit} (clean)")
            if s.target == "kind":
                pushed = d.push_image(c.image_ref(), s.registry)
                typer.echo(f"pushed {pushed.ref} (image id {pushed.config_digest})")
            else:
                pushed = d.resolve_pushed(c.image_ref(), s.registry, s.gke.gcloud)
                typer.echo(f"Artifact Registry holds {pushed.ref} (id {pushed.config_digest})")
            write_rendered(settings, pushed.ref)
            d.apply_pool(s.kubectl, s.context, s.deploy_dir / POOL_FILE)
            d.wait_pool(s.kubectl, s.context, s.namespace, s.worker_pool, timeout=300)
            client = c.substrate_admin_client()
            d.ensure_atespace(client, s.kubectl, s.context)
            outcome = d.ensure_access_policy(
                s.kubectl,
                s.context,
                s.atespace,
                s.deploy_dir / ACCESS_POLICY_FILE,
                access_policy_spec(access_principals(settings))["bindings"],
            )
            typer.echo(f"access policy (the dispatcher only): {outcome}")
            names = [
                d.create_template(client, s.kubectl, s.context, template_path(s.deploy_dir, p))
                for p in (*SUBSTRATE_POLICIES, CONFORMANCE)
            ]
            for name, seconds in d.wait_golden(client, names, timeout=600).items():
                typer.echo(f"golden snapshot of {name} after {seconds} s")
            if prune:
                for name in d.prune_templates(client, s.kubectl, s.context, names):
                    typer.echo(f"deleted template {name}")
        except (d.DeployError, SubstrateError, ExecutionError, ImageError, OSError) as exc:
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
    """Containment checks in real actors for each substrate policy, run by the dispatcher
    (POST /v1/conformance); ledger events as for OpenShell (PolicyDenied,
    ContainmentCheckCompleted with executor substrate)."""
    from meridian.sandbox import substrate_conformance as sc
    from meridian.sandbox.substrate_deploy import image_config_digest
    from meridian.sandbox.substrate_executor import load_template
    from meridian.sandbox.substrate_templates import CONFORMANCE

    with Container.build(Settings()) as c:
        log = _log(c)
        s = c.settings.substrate
        failed = False
        try:
            executor = c.substrate_executor(log)
            client = c.dispatcher_client()
            probe = load_template(client, s.deploy_dir, CONFORMANCE, probe=True)
            digest = image_config_digest(probe.image, s.target)
            for zone in zones or c.substrate_policies():
                template = executor.template_for_policy(zone)
                result = sc.run_substrate_conformance(
                    session=client.conformance,
                    zone=zone,
                    zone_template=template,
                    probe_template=probe,
                    placement=executor.placement,
                    image_digest=digest,
                    kubectl=s.kubectl,
                    context=s.context,
                )
                verdict = "pass" if result.passed else "FAIL"
                typer.echo(
                    f"{verdict}  {zone} (actor {result.actor}, peer {result.peer_ip}, "
                    f"dispatcher {result.dispatcher}, API {result.kube_api[0]})"
                )
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
        "runs_each": sb.each(results),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    shown = {k: v for k, v in line.items() if k not in ("wall_ms_each", "runs_each")}
    typer.echo(json.dumps(shown, indent=1)[:4000])
    if line["failed"]:
        raise typer.Exit(code=1)


@substrate_app.command("placement-check")
def substrate_placement_check(
    taint_node: Annotated[
        bool,
        typer.Option(
            "--taint-node/--no-taint-node",
            help="kind: taint the labelled node for the check, restart the workers, then "
            "remove the taint (GKE's worker pool is tainted for good).",
        ),
    ] = False,
    label_kind_node: Annotated[
        bool, typer.Option("--label-kind-node", help="kind: first put the worker label on it.")
    ] = False,
) -> None:
    """Workers only on nodes with the worker label, tolerating the worker taint; a pod
    without the toleration refused there (meridian.sandbox.placement)."""
    from meridian.sandbox import placement as pl

    s = Settings().substrate
    try:
        if label_kind_node:
            if s.target != "kind":
                raise pl.PlacementError("--label-kind-node is for kind")
            typer.echo(pl.label_node(s, "kind-control-plane"))
        result = pl.check(s, taint_node, "workload=meridian-dispatcher")
    except pl.PlacementError as exc:
        raise _fail(exc) from exc
    for line in pl.summary(result.checks):
        typer.echo(line)
    if not result.passed:
        raise typer.Exit(code=1)


@substrate_app.command("guard-check")
def substrate_guard_check() -> None:
    """Phase 4a's proof for the laptop's runner identity, which phase 4b step 3 removed.
    The dispatcher's refusals are tests/unit/test_dispatcher.py and its client's allow-list
    (DISPATCHER_VERBS); docs/substrate-eval.md keeps the phase 4a results."""
    typer.echo(
        "guard-check proved the laptop's runner identity (phase 4a), which no longer exists: "
        "only the dispatcher may act in the atespace now. See docs/substrate-eval.md.",
        err=True,
    )
    raise typer.Exit(code=2)

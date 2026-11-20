"""`meridian dispatcher ...`: the in-cluster dispatcher (phase 4b step 2).

token makes the shared token (a 0600 file outside the repository, and its Secret); deploy
builds, pushes and applies the dispatcher and gives it its identity; status reaches it
through the tunnel. With [substrate] via = "dispatcher", every Substrate run of
`agents run`, `audit review` and the `substrate` proofs goes through it.
"""

import hashlib
import shutil
from pathlib import Path
from typing import Annotated

import typer

from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.substrate import SubstrateError

dispatcher_app = typer.Typer(
    no_args_is_help=True, help="The in-cluster dispatcher for Substrate runs (phase 4b)."
)


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(code=1)


def _kubectl_ate() -> Path:
    found = shutil.which("kubectl-ate")
    if found is None:
        raise typer.BadParameter("kubectl-ate is not on PATH (build it from Substrate v1.0.0)")
    return Path(found).resolve()


@dispatcher_app.command("token")
def dispatcher_token(
    rotate: Annotated[bool, typer.Option("--rotate", help="Replace an existing token.")] = False,
) -> None:
    """Make the shared token (if none, or with --rotate) and store it in the Secret. The
    token is written only to [substrate] dispatcher_token_file (0600) and the Secret."""
    from meridian.sandbox import dispatch
    from meridian.sandbox.dispatcher_deploy import (
        DispatcherDeployError,
        apply_token_secret,
        namespace_exists,
    )
    from meridian.sandbox.dispatcher_executor import DispatcherError, read_token, write_token

    s = Settings().substrate
    path = s.dispatcher_token_file.expanduser()
    try:
        if rotate or not path.exists():
            write_token(path, dispatch.new_token())
            typer.echo(f"wrote a new token to {path} (0600)")
        read_token(path)  # refuses a group- or world-readable file
        if not namespace_exists(s.kubectl, s.context, s.dispatcher_namespace):
            typer.echo(
                f"namespace {s.dispatcher_namespace} does not exist yet: "
                "`meridian dispatcher deploy` applies the Secret"
            )
            return
        apply_token_secret(s.kubectl, s.context, s, path)
    except (DispatcherError, DispatcherDeployError, OSError) as exc:
        raise _fail(exc) from exc
    typer.echo(f"secret {s.dispatcher_namespace}/meridian-dispatcher-token applied")
    typer.echo("restart the dispatcher to read a rotated token: meridian dispatcher deploy")


@dispatcher_app.command("deploy")
def dispatcher_deploy() -> None:
    """kind: build the dispatcher image from a clean tree and push it to the kind registry.
    GKE: take the image you pushed to Artifact Registry, by digest (no build). Then render
    [substrate] dispatcher_deploy_dir for that digest, apply it with the token Secret, make
    the dispatcher the only member of the atespace's access policy and of atenet-router's
    allow-list (kind: patched; GKE: checked), on GKE check that no IAM grant names it, and
    wait for it."""
    from meridian.cli_substrate import access_principals
    from meridian.sandbox import dispatcher_deploy as dd
    from meridian.sandbox import gke, image
    from meridian.sandbox import substrate_deploy as d
    from meridian.sandbox.dispatcher_executor import DispatcherError, read_token
    from meridian.sandbox.image import ImageError
    from meridian.sandbox.substrate_templates import ACCESS_POLICY_FILE, access_policy_spec

    settings = Settings()
    s = settings.substrate
    binary = _kubectl_ate()
    binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    with Container.build(settings) as c:
        try:
            c.check_substrate_release()
            read_token(s.dispatcher_token_file)
            local = f"{dd.IMAGE_NAME}:{image.package_version()}"
            if s.target == "kind":
                info = image.build(
                    local,
                    dockerfile=dd.DOCKERFILE,
                    extra_args=["--build-context", f"ate={binary.parent}"],
                )
            else:
                info = image.inspect(local)
            commit = d.require_clean_source(info)
            typer.echo(f"{local} built from {commit} (clean), kubectl-ate sha256 {binary_sha}")
            if s.target == "kind":
                pushed = d.push_image(local, s.registry)
                typer.echo(f"pushed {pushed.ref}")
            else:
                pushed = d.resolve_pushed(local, s.registry, s.gke.gcloud)
                typer.echo(f"Artifact Registry holds {pushed.ref}")
                grants, read = gke.dispatcher_bindings(
                    gke.run_gcloud(s.gke.gcloud),
                    s.gke,
                    s.dispatcher_namespace,
                    s.dispatcher_service_account,
                )
                if grants:
                    raise DispatcherError(f"IAM grants name the dispatcher: {grants}")
                who = s.gke.workload_principal(s.dispatcher_namespace, s.dispatcher_service_account)
                typer.echo(f"Workload Identity: {who} holds no role ({len(read)} policies read)")
            out = s.dispatcher_deploy_dir / dd.MANIFEST
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(dd.render(s, pushed.ref, binary_sha), encoding="utf-8")
            typer.echo(f"wrote {out}")
            dd._run([s.kubectl, "--context", s.context, "apply", "-f", str(out)])
            dd.apply_token_secret(s.kubectl, s.context, s, s.dispatcher_token_file)
            outcome = d.ensure_access_policy(
                s.kubectl,
                s.context,
                s.atespace,
                s.deploy_dir / ACCESS_POLICY_FILE,
                access_policy_spec(access_principals(settings))["bindings"],
            )
            typer.echo(f"atespace access policy (the dispatcher only): {outcome}")
            router = (
                dd.set_router_ingress(s.kubectl, s.context, s)
                if s.target == "kind"
                else dd.require_router_only_dispatcher(s.kubectl, s.context, s)
            )
            typer.echo(f"router ingress allow-list: {router}")
            dd._run(
                [
                    s.kubectl,
                    "--context",
                    s.context,
                    "-n",
                    s.dispatcher_namespace,
                    "rollout",
                    "restart",
                    f"deployment/{s.dispatcher_deployment}",
                ]
            )
            d.wait_pool(s.kubectl, s.context, s.dispatcher_namespace, s.dispatcher_deployment, 300)
            typer.echo("dispatcher ready")
        except (
            dd.DispatcherDeployError,
            d.DeployError,
            DispatcherError,
            ImageError,
            gke.GcloudError,
            OSError,
        ) as exc:
            raise _fail(exc) from exc


@dispatcher_app.command("status")
def dispatcher_status() -> None:
    """Reach the dispatcher through the tunnel: free workers, the zone templates and their
    golden snapshots as it sees them, live actors."""
    from meridian.sandbox.substrate_executor import load_template
    from meridian.sandbox.substrate_templates import SUBSTRATE_POLICIES

    with Container.build(Settings()) as c:
        try:
            client = c.dispatcher_client()
            typer.echo(f"tunnel {c.dispatcher_tunnel().address()} (one port-forward)")
            typer.echo(f"free workers {client.free_workers()}")
            for policy in SUBSTRATE_POLICIES:
                t = load_template(client, c.settings.substrate.deploy_dir, policy)
                typer.echo(f"{policy:<22} {t.name:<36} golden {t.golden_snapshot}")
            typer.echo(f"live actors: {len(client.actors())}")
        except (SubstrateError, ExecutionError) as exc:
            raise _fail(exc) from exc

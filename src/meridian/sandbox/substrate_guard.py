"""`meridian substrate guard-check`: the runner may not suspend, revert or tag an actor.

Substrate v1.0.0 cannot refuse this by itself. Creating and deleting actors needs `editor`
on the atespace, and an editor may also suspend, revert and tag them: access policies take
only the roles owner, editor and viewer (cmd/ateapi/internal/apivalidation/access_policy.go),
`can_suspend` and `can_revert` are `editor from parent_atespace` and `can_create_tag` is
`editor` (cmd/ateapi/internal/authz/model.fga), and there are no per-actor or per-template
grants (docs/guides/authorization.md, "Known limitations"). So the check proves three
layers, each recorded in the ledger:

1. The runner's own client refuses suspend, revert and tag before anything is sent (the
   verb allow-list in meridian.sandbox.substrate): PolicyDenied, source runner_guard.
2. Substrate refuses the runner's identity everything outside its atespace (it is not the
   shared bootstrap owner any more): PolicyDenied, source substrate_authz, with the
   server's PermissionDenied text.
3. After the refused attempts the actor still holds only its golden snapshot, read back
   from Substrate (the per-run check every run makes).

With show_gap, the same identity without the client guard suspends a data-free actor of
the conformance template, to show the gap is real; that outcome is recorded too.
"""

import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Literal

from meridian.domain.enums import Visibility
from meridian.domain.events import EventDraft, PolicyDenied
from meridian.platform.clock import Clock
from meridian.sandbox.substrate import (
    GuardRefusedError,
    KubectlAteClient,
    NotFoundError,
    SubstrateError,
)
from meridian.sandbox.substrate_executor import ZoneTemplate, foreign_snapshots

ACTOR = "system.runner_guard"
GUARD_POLICY = "src/meridian/sandbox/substrate.py RUNNER_VERBS"
AUTHZ_POLICY = "deploy/substrate/access-policy.yaml"

Outcome = Literal["refused", "allowed"]


@dataclass(frozen=True, slots=True)
class Attempt:
    check: str
    layer: Literal["runner_guard", "substrate_authz", "gap"]
    command: str
    outcome: Outcome
    message: str


@dataclass(slots=True)
class GuardCheck:
    actor: str
    template: str
    attempts: list[Attempt] = field(default_factory=list)
    state_after: str | None = None
    foreign_after: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        expected = all(
            a.outcome == ("allowed" if a.layer == "gap" else "refused") for a in self.attempts
        )
        guarded = {a.check for a in self.attempts if a.layer == "runner_guard"}
        return (
            expected
            and {"suspend", "revert", "tag"} <= guarded
            and any(a.layer == "substrate_authz" for a in self.attempts)
            and self.state_after == "ACTOR_STATE_RUNNING"
            and not self.foreign_after
        )


def _guarded(check: str, command: str, call: object) -> Attempt:
    try:
        assert callable(call)
        call()
    except GuardRefusedError as exc:
        return Attempt(check, "runner_guard", command, "refused", str(exc))
    except SubstrateError as exc:  # reached Substrate: the guard did not hold
        return Attempt(check, "runner_guard", command, "allowed", f"sent: {exc}")
    return Attempt(check, "runner_guard", command, "allowed", "sent and accepted")


def _authz(client: KubectlAteClient, check: str, args: list[str]) -> Attempt:
    command = "kubectl ate " + " ".join(args)
    try:
        client.call(args)
    except SubstrateError as exc:
        text = str(exc)
        outcome: Outcome = "refused" if "PermissionDenied" in text else "allowed"
        return Attempt(check, "substrate_authz", command, outcome, text[-600:])
    return Attempt(check, "substrate_authz", command, "allowed", "accepted")


def run_guard_check(
    *,
    runner: KubectlAteClient,
    runner_unguarded: KubectlAteClient,
    template: ZoneTemplate,
    probe_template: ZoneTemplate,
    show_gap: bool,
) -> GuardCheck:
    """runner is the runner's client (identity and guard); runner_unguarded is the same
    identity without the guard, used only for the Substrate-side checks."""
    if not runner.guarded or runner_unguarded.guarded:
        raise ValueError("runner must carry the guard and runner_unguarded must not")
    actor = f"mer-gc-{uuid.uuid4().hex[:8]}"
    result = GuardCheck(actor=actor, template=template.name)
    try:
        runner.create_actor(actor, template.name)
        runner.resume_actor(actor)
        result.attempts += [
            _guarded(
                "suspend", f"kubectl ate suspend actor {actor}", lambda: runner.suspend_actor(actor)
            ),
            _guarded(
                "revert", f"kubectl ate revert actor {actor}", lambda: runner.revert_actor(actor)
            ),
            _guarded(
                "tag",
                f"kubectl ate create tag mer-gc-tag --actor {actor}",
                lambda: runner.tag_actor(actor, f"{actor}-tag"),
            ),
        ]
        final = runner.get_actor(actor) or {}
        result.state_after = str(dict(final.get("status", {})).get("state"))
        result.foreign_after = foreign_snapshots(final, template.golden_snapshot)
    finally:
        with contextlib.suppress(NotFoundError):
            runner.delete_actor(actor)
    result.attempts += [
        _authz(runner_unguarded, "list_atespaces", ["get", "atespace"]),
        _authz(runner_unguarded, "list_workers", ["get", "worker"]),
        _authz(runner_unguarded, "other_atespace", ["get", "actor", "-a", "ate-demo"]),
        _authz(
            runner_unguarded,
            "access_policy",
            ["delete", "access-policy", "-a", runner_unguarded.atespace],
        ),
    ]
    if show_gap:
        result.attempts.append(_show_gap(runner_unguarded, probe_template))
    return result


def _show_gap(client: KubectlAteClient, probe: ZoneTemplate) -> Attempt:
    """Suspend a data-free conformance actor as the runner's identity without the guard."""
    actor = f"mer-gap-{uuid.uuid4().hex[:8]}"
    command = f"kubectl ate suspend actor {actor} (runner identity, no guard, no data)"
    try:
        client.create_actor(actor, probe.name)
        client.resume_actor(actor)
        state = dict(client.suspend_actor(actor).get("status", {})).get("state")
        return Attempt("suspend", "gap", command, "allowed", f"Substrate accepted: {state}")
    except SubstrateError as exc:
        return Attempt("suspend", "gap", command, "refused", str(exc)[-600:])
    finally:
        with contextlib.suppress(SubstrateError):
            client.delete_actor(actor)


def ledger_drafts(result: GuardCheck, clock: Clock) -> list[EventDraft]:
    """A PolicyDenied for every refusal. The gap attempt is not a refusal; it is reported
    by the command and in docs/substrate-design.md, not recorded as one."""
    now = clock.now()
    drafts = []
    for a in result.attempts:
        if a.outcome != "refused":
            continue
        drafts.append(
            EventDraft(
                occurred_at=now,
                actor=ACTOR,
                visibility=Visibility.BLINDED,
                payload=PolicyDenied(
                    agent_id=ACTOR,
                    resource=a.command,
                    policy=GUARD_POLICY if a.layer == "runner_guard" else AUTHZ_POLICY,
                    reason=a.message[:500],
                    source="runner_guard" if a.layer == "runner_guard" else "substrate_authz",
                    check=a.check,
                    sandbox=result.actor if a.layer == "runner_guard" else None,
                    raw_line=a.message if a.layer == "substrate_authz" else None,
                ),
            )
        )
    return drafts

"""The runner's Substrate identity and client guard (phase 4a step 3, item 1), without a
cluster: subprocess is replaced, so a refused call is one that never started."""

import json
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from meridian.cli_substrate import access_principals
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.sandbox import substrate_guard as sg
from meridian.sandbox.substrate import (
    RUNNER_VERBS,
    GuardRefusedError,
    KubectlAteClient,
    ServiceAccountToken,
)
from meridian.sandbox.substrate_templates import ACCESS_POLICY_FILE, render_access_policy

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "substrate"


class _Recorder:
    def __init__(self, stdout: str = "{}") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    return rec


def _client(token: Path | None = None) -> KubectlAteClient:
    return KubectlAteClient(
        "meridian",
        context="kind-kind",
        token_file=(lambda: token) if token else None,
        allowed=RUNNER_VERBS,
    )


@pytest.mark.parametrize(
    ("call", "verb"),
    [
        (lambda c: c.suspend_actor("mer-x"), "suspend actor"),
        (lambda c: c.revert_actor("mer-x"), "revert actor"),
        (lambda c: c.tag_actor("mer-x", "t"), "create tag"),
        (lambda c: c.call(["update", "actor", "mer-x"]), "update actor"),
        (lambda c: c.call(["create", "actor-template", "-f", "x.yaml"]), "create actor-template"),
        (lambda c: c.call(["delete", "access-policy", "-a", "meridian"]), "delete access-policy"),
    ],
)
def test_the_runner_client_refuses_before_anything_is_sent(
    recorder: _Recorder, call: Any, verb: str
) -> None:
    with pytest.raises(GuardRefusedError, match=verb):
        call(_client())
    assert recorder.calls == []


def test_every_call_a_run_makes_passes_the_guard_with_the_runner_token(
    recorder: _Recorder, tmp_path: Path
) -> None:
    token = tmp_path / "runner.token"
    client = _client(token)
    client.get_template("t")
    client.get_tag("g")
    client.get_actor("a")
    client.actors()
    client.create_actor("a", "t")
    client.resume_actor("a")
    client.delete_actor("a")
    client._guard(["proxy", "actor", "a"])  # proxy is checked before Popen
    verbs = {(c[6], c[7]) for c in recorder.calls}
    assert verbs <= RUNNER_VERBS
    assert all(c[4:6] == ["--token-file", str(token)] for c in recorder.calls)


def test_the_allow_list_has_no_verb_that_writes_a_snapshot() -> None:
    assert {v for v, _ in RUNNER_VERBS} == {"get", "create", "resume", "proxy", "delete"}
    assert ("create", "tag") not in RUNNER_VERBS
    assert {k for v, k in RUNNER_VERBS if v != "get"} == {"actor"}


def test_the_admin_client_is_not_guarded(recorder: _Recorder) -> None:
    KubectlAteClient("meridian").suspend_actor("mer-x")
    assert recorder.calls[0][2:4] == ["suspend", "actor"]


def test_the_token_is_minted_once_into_a_private_file(recorder: _Recorder, tmp_path: Path) -> None:
    recorder.stdout = "header.payload.signature\n"
    token = ServiceAccountToken(
        path=tmp_path / "var" / "runner.token",
        namespace="meridian",
        service_account="meridian-runner",
        audience="api.ate-system.svc",
        context="kind-kind",
    )
    path = token()
    assert token() == path
    assert len(recorder.calls) == 1
    argv = recorder.calls[0]
    assert argv[argv.index("token") + 1] == "meridian-runner"
    assert argv[argv.index("--audience") + 1] == "api.ate-system.svc"
    assert path.read_text() == "header.payload.signature"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_committed_access_policy_names_only_the_dispatcher() -> None:
    s = Settings().substrate
    assert (DEPLOY / ACCESS_POLICY_FILE).read_text() == render_access_policy(
        s.atespace, access_principals(Settings())
    )
    text = (DEPLOY / ACCESS_POLICY_FILE).read_text()
    assert "role: editor" in text
    # Phase 4b step 3: the dispatcher alone, editor on the atespace only; the laptop's
    # runner identity of phase 4a is gone.
    assert "meridian-runner" not in text
    assert "serviceaccount:meridian-dispatch:dispatcher" in text
    assert text.count('- "user:') == 1
    assert "owner" not in text.split("bindings:")[1]
    assert not (DEPLOY / "runner.yaml").exists()


def _check(*attempts: sg.Attempt, state: str = "ACTOR_STATE_RUNNING") -> sg.GuardCheck:
    return sg.GuardCheck(
        actor="mer-gc-1", template="rbqm-x", attempts=list(attempts), state_after=state
    )


GUARD = [
    sg.Attempt(c, "runner_guard", f"kubectl ate {c}", "refused", "refused by the runner guard")
    for c in ("suspend", "revert", "tag")
]
AUTHZ = sg.Attempt(
    "list_workers", "substrate_authz", "kubectl ate get worker", "refused", "PermissionDenied: x"
)


def test_the_check_passes_only_if_every_layer_refuses() -> None:
    assert _check(*GUARD, AUTHZ).passed
    gap = sg.Attempt("suspend", "gap", "suspend (no guard)", "allowed", "accepted")
    assert _check(*GUARD, AUTHZ, gap).passed
    leaked = sg.Attempt("suspend", "runner_guard", "x", "allowed", "sent")
    assert not _check(leaked, *GUARD[1:], AUTHZ).passed
    assert not _check(*GUARD).passed
    assert not _check(*GUARD, AUTHZ, state="ACTOR_STATE_SUSPENDED").passed


def test_each_refusal_is_one_policy_denied_with_its_layer() -> None:
    gap = sg.Attempt("suspend", "gap", "suspend (no guard)", "allowed", "accepted")
    drafts = sg.ledger_drafts(
        _check(*GUARD, AUTHZ, gap), FixedClock(datetime(2026, 11, 11, tzinfo=UTC))
    )
    sources = [d.payload.source for d in drafts]  # type: ignore[union-attr]
    assert sources == ["runner_guard"] * 3 + ["substrate_authz"]
    authz = drafts[-1].payload
    assert authz.raw_line == "PermissionDenied: x"  # type: ignore[union-attr]
    assert json.loads(drafts[0].payload.model_dump_json())["sandbox"] == "mer-gc-1"

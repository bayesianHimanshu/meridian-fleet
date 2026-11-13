"""The in-cluster dispatcher and the laptop's DispatcherExecutor (phase 4b step 2).

The dispatcher's real HTTP server runs on 127.0.0.1 with a fake Substrate whose actors
run meridian-worker serve's code in process (tests.unit.test_substrate_executor), so a
run goes laptop staging -> authenticated request -> dispatcher -> actor -> back, with only
the cluster missing.
"""

import contextlib
import dataclasses
import http.server
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.data.study_store import ParquetStudyReader
from meridian.dispatcher_serve import Dispatcher, make_handler
from meridian.platform.clock import FixedClock
from meridian.platform.config import SubstrateSettings
from meridian.sandbox import dispatch
from meridian.sandbox.dispatcher_executor import (
    DispatcherClient,
    DispatcherError,
    DispatcherExecutor,
    read_token,
    write_token,
)
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.substrate import (
    DISPATCHER_VERBS,
    GuardRefusedError,
    KubectlAteClient,
)
from meridian.sandbox.substrate_executor import WorkerLostError, archive_members
from meridian.sandbox.substrate_templates import SUBSTRATE_POLICIES
from meridian.sandbox.tracking import SandboxTracker
from tests.rbqm_support import NOW, STUDY
from tests.unit.test_substrate_executor import AGENTS, GOLDEN, FakeActorClient, _template
from tests.worker_support import cut_trigger, tiny_cut

TOKEN = "t" * 48
DISPATCHER_IMAGE = "localhost:5001/meridian-dispatcher@sha256:" + "e" * 64


class FakeSubstrate(FakeActorClient):
    """FakeActorClient that also answers template and tag reads with the golden tag."""

    lose_worker: bool = False

    def get_template(self, name: str) -> dict[str, Any]:
        return {"status": {"goldenSnapshotStatus": {"goldenTag": {"name": "golden-tag"}}}}

    def get_tag(self, name: str) -> dict[str, Any]:
        return {"status": {"snapshot": {"uuid": GOLDEN, "owner": "SNAPSHOT_OWNER_TAG"}}}

    def get_actor(self, name: str) -> dict[str, Any] | None:
        actor = super().get_actor(name)
        if actor is not None and self.lose_worker:
            actor["status"] = {"state": "ACTOR_STATE_CRASHED", "snapshots": self.snapshots}
        elif actor is not None:
            actor["status"]["workerAssignment"]["workerPodIps"] = ["10.0.0.7"]
        return actor

    def resume_actor(self, name: str) -> dict[str, Any]:
        super().resume_actor(name)
        return {"status": {"workerAssignment": {"workerPod": "meridian-abc", "nodeName": "n1"}}}


def _send(fake: FakeSubstrate) -> Callable[..., httpx.Response]:
    def send(
        actor: str, body: bytes, headers: Mapping[str, str], lap: Callable[[str], None]
    ) -> httpx.Response:
        if fake.lose_worker:
            raise httpx.RemoteProtocolError("connection reset by the router")
        lap("connect")
        return fake.post(
            f"http://actor-{actor}:8080/run", content=body, headers=dict(headers), timeout=5
        )

    return send


@contextlib.contextmanager
def _server(fake: FakeSubstrate, token: str = TOKEN) -> Iterator[tuple[str, Dispatcher]]:
    dispatcher = Dispatcher(
        client=fake, send=_send(fake), placement_wait=1.0, ready_workers=lambda: 2
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(dispatcher, lambda: token, structlog.get_logger())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", dispatcher
    finally:
        server.shutdown()
        server.server_close()


def _executor(roots: Path, fake: FakeSubstrate, address: str) -> DispatcherExecutor:
    templates = {p: _template(p) for p in SUBSTRATE_POLICIES}
    fake.templates = {t.name: p for p, t in templates.items()}
    return DispatcherExecutor(
        dispatcher=DispatcherClient(address=lambda: address, token=TOKEN, timeout=10),
        dispatcher_image=DISPATCHER_IMAGE,
        registry=build_registry(AGENTS),
        settings=SubstrateSettings(),
        templates=templates,
        image_digest="sha256:" + "d" * 64,
        study_root=roots / "curated" / STUDY,
        tracker=SandboxTracker(roots / "actors.jsonl", FixedClock(NOW)),
        log=structlog.get_logger(),
    )


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    tiny_cut(tmp_path / "curated")
    return tmp_path


def test_a_dispatched_run_gives_the_same_drafts_and_deletes_its_actor(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, _):
        executor = _executor(roots, fake, address)
        plan = executor.plan("rbqm.kri_monitor", cut_trigger())
        assert plan.substrate is not None
        assert plan.substrate.dispatcher_image == DISPATCHER_IMAGE
        result = executor.run("rbqm.kri_monitor", cut_trigger(), "DC-TINY", plan)
        assert executor.capacity("substrate") == 2  # ready workers, none busy
    registered = build_registry(AGENTS).get("rbqm.kri_monitor")
    reader = ParquetStudyReader(roots / "curated" / STUDY, "DC-TINY")
    expected = drafts_document(
        registered, run_agent(registered, cut_trigger(), reader, structlog.get_logger())
    )
    assert drafts_document(registered, result.drafts) == expected
    actor = plan.substrate.actor
    assert fake.created == [actor]
    assert fake.deleted == [actor, actor]  # the pre-delete (retry safety), then the real one
    assert fake.live == {}
    assert result.substrate is not None
    assert result.substrate.golden_snapshot == GOLDEN
    assert result.substrate.dispatcher_image == DISPATCHER_IMAGE
    assert result.inputs_manifest_sha256 is not None
    steps = [step for step, _ in result.phases_ms]
    assert steps == ["stage", "create", "resume", "connect", "request", "delete", "tunnel"]
    # C4 through the dispatcher: the actor received the laptop's archive, only the zone's
    # areas (rbqm: meta, blinded, ops), byte for byte (the worker checked its sha256).
    (received,) = fake.received
    assert {m.split("/", 1)[0] for m in archive_members(received)} == {"meta", "blinded", "ops"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer " + "x" * 48, dispatch.NONCE_HEADER: "n"},
        {"Authorization": "Bearer " + TOKEN},  # no nonce
        {"Authorization": TOKEN, dispatch.NONCE_HEADER: "n"},  # not a bearer token
    ],
)
def test_an_unauthenticated_request_is_refused_before_anything_runs(
    headers: dict[str, str],
) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, _):
        for method, path in [
            ("GET", "/v1/templates/x"),
            ("GET", "/v1/actors"),
            ("POST", "/v1/runs"),
            ("DELETE", "/v1/actors/x"),
        ]:
            response = httpx.request(
                method, f"http://{address}{path}", headers=headers, content=b"x" * 10
            )
            assert response.status_code == 401
            assert dispatch.PROOF_HEADER not in response.headers
        assert httpx.get(f"http://{address}/healthz").status_code == 200
    assert fake.created == []
    assert fake.deleted == []


def test_the_laptop_refuses_an_answer_without_a_valid_proof() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"actors": []}, headers={dispatch.PROOF_HEADER: "0" * 64})

    client = DispatcherClient(
        address=lambda: "dispatcher",
        token=TOKEN,
        timeout=5,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(DispatcherError, match="no valid proof"):
        client.actors()


def test_a_wrong_token_on_the_laptop_is_reported_as_refused(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake, token="s" * 48) as (address, _):
        client = DispatcherClient(address=lambda: address, token=TOKEN, timeout=5)
        with pytest.raises(DispatcherError, match="401"):
            client.actors()


def test_a_lost_worker_is_reported_and_raises_worker_lost(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    fake.lose_worker = True
    with _server(fake) as (address, _):
        executor = _executor(roots, fake, address)
        with pytest.raises(WorkerLostError) as lost:
            executor.run("rbqm.kri_monitor", cut_trigger(), "DC-TINY")
    assert (lost.value.worker_pod, lost.value.node) == ("meridian-abc", "n1")
    assert fake.live == {}  # deleted all the same


def test_a_snapshot_other_than_the_golden_one_fails_the_run(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    fake.snapshots = [*fake.snapshots, {"uuid": "data-snapshot", "owner": "SNAPSHOT_OWNER_ACTOR"}]
    with _server(fake) as (address, _):
        executor = _executor(roots, fake, address)
        with pytest.raises(ExecutionError, match="other than"):
            executor.run("rbqm.kri_monitor", cut_trigger(), "DC-TINY")
    assert fake.live == {}


def test_the_dispatcher_client_refuses_suspend_revert_tag_and_proxy() -> None:
    client = KubectlAteClient(
        "meridian", argv0=["/nonexistent/kubectl-ate"], allowed=DISPATCHER_VERBS
    )
    refused: list[Callable[[], object]] = [
        lambda: client.suspend_actor("a"),
        lambda: client.revert_actor("a"),
        lambda: client.tag_actor("a", "t"),
        lambda: client.call(["proxy", "actor", "a"]),
    ]
    for call in refused:
        with pytest.raises(GuardRefusedError):
            call()  # refused before the (missing) binary is run


def test_the_laptop_cannot_drive_actors_itself() -> None:
    client = DispatcherClient(address=lambda: "x", token=TOKEN, timeout=1)
    calls: list[Callable[[], object]] = [
        lambda: client.create_actor("a", "t"),
        lambda: client.resume_actor("a"),
        lambda: client.suspend_actor("a"),
        lambda: client.revert_actor("a"),
        lambda: client.tag_actor("a", "t"),
        lambda: client.proxy("a", 8080),
    ]
    for call in calls:
        with pytest.raises(GuardRefusedError):
            call()


def test_the_token_file_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "dispatcher.token"
    write_token(path, TOKEN)
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_token(path) == TOKEN
    path.chmod(0o644)
    with pytest.raises(DispatcherError, match="owner only"):
        read_token(path)
    path.chmod(0o600)
    path.write_text("short")
    with pytest.raises(DispatcherError, match="too short"):
        read_token(path)


def test_the_proof_binds_the_nonce_and_the_body() -> None:
    p = dispatch.proof(TOKEN, "nonce", b"body")
    assert dispatch.proof_ok(TOKEN, "nonce", b"body", p)
    assert not dispatch.proof_ok(TOKEN, "other", b"body", p)
    assert not dispatch.proof_ok(TOKEN, "nonce", b"bodx", p)
    assert not dispatch.proof_ok("u" * 48, "nonce", b"body", p)
    assert not dispatch.proof_ok(TOKEN, "nonce", b"body", None)


def _probe_answers(actor: str, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
    """A conformance actor as meridian.worker_probe answers, with egress default deny."""
    import json

    if method == "GET":
        return 200, b"ok"
    check = json.loads(body or b"{}")
    if check["check"] == "tcp_connect" and check["host"] == "127.0.0.1":
        answer = {"outcome": "ok", "connected": True, "reply": "HTTP/1.0 200 OK"}
    elif check["check"] == "tcp_connect":
        answer = {"outcome": "ok", "connected": True, "reply": "HTTP/1.1 400 Bad Request"}
    elif check["check"] == "resolve":
        answer = {"outcome": "unreachable"}
    elif check["check"] == "write" and check["path"].startswith("/opt"):
        answer = {"outcome": "refused", "errno": 13}
    else:
        answer = {"outcome": "ok"}
    return 200, json.dumps(answer).encode()


def test_conformance_runs_through_the_dispatcher_on_the_conformance_template_only(
    roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.sandbox import substrate_conformance as sc

    monkeypatch.setattr(sc, "gateway_lines", lambda *a, **k: ())
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, dispatcher):
        dispatcher.request = _probe_answers
        dispatcher.own_address = "10.0.0.9:8090"
        dispatcher.kube_api = ("34.118.224.1", 443)
        executor = _executor(roots, fake, address)
        client = DispatcherClient(address=lambda: address, token=TOKEN, timeout=10)
        probe = dataclasses.replace(_template("rbqm"), name="conformance-cccc-m384")
        result = sc.run_substrate_conformance(
            session=client.conformance,
            zone="rbqm",
            zone_template=executor.template_for_policy("rbqm"),
            probe_template=probe,
            placement=executor.placement,
            image_digest="sha256:" + "d" * 64,
            kubectl="kubectl",
            context="kind-kind",
        )
        with pytest.raises(DispatcherError, match="400"):
            client.conformance(executor.template_for_policy("rbqm").name)
    assert [c.name for c in result.checks if not c.passed] == []
    names = {c.name for c in result.checks}
    assert {"egress_dispatcher", "egress_metadata", "egress_kube_api"} <= names
    kube = next(c for c in result.checks if c.name == "egress_kube_api")
    assert "34.118.224.1" in kube.command
    assert result.dispatcher == "10.0.0.9:8090"
    assert result.peer_ip == "10.0.0.7"
    assert len(fake.created) == 2  # the probe and its peer, nothing for the zone template
    assert fake.live == {}  # both deleted

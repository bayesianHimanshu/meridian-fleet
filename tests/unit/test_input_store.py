"""Inputs by content (phase 4b step 4, docs/input-staging.md): the store, the archive
rebuilt from a RunRef, and a pass through the real dispatcher HTTP server whose inputs
go up once per distinct file and are gone after the pass."""

import hashlib
from pathlib import Path

import httpx
import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.data.study_store import ParquetStudyReader
from meridian.sandbox import dispatch
from meridian.sandbox.input_store import (
    InputStoreError,
    MemoryInputStore,
    MissingInputsError,
    RunRef,
    archive_entries,
    archive_from,
    assemble,
    sha256,
    split,
)
from meridian.sandbox.substrate_executor import archive
from tests.rbqm_support import STUDY
from tests.unit.test_dispatcher import TOKEN, FakeSubstrate, _executor, _server
from tests.unit.test_substrate_executor import AGENTS
from tests.worker_support import cut_trigger, tiny_cut


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    tiny_cut(tmp_path / "curated")
    return tmp_path


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    for area, files in {
        "meta": {"trigger.json": b"{}"},
        "blinded": {"a.parquet": b"A" * 300, "b.parquet": b"B" * 70},
    }.items():
        (stage / area).mkdir(parents=True)
        for name, data in files.items():
            (stage / area / name).write_bytes(data)
    return stage


def test_a_ref_rebuilds_the_laptops_archive_byte_for_byte(tmp_path: Path) -> None:
    body = archive(_stage(tmp_path))
    assert archive_from(archive_entries(body)) == body
    ref, blobs = split(body)
    assert [m.path for m in ref.members] == ["blinded/a.parquet", "blinded/b.parquet"]
    assert set(ref.inline) == {"meta/trigger.json"}  # meta/ goes inline, never by digest
    store = MemoryInputStore()
    for digest, data in blobs.items():
        store.put(digest, data)
    assert assemble(ref, store.get) == body
    assert ref.archive_sha256 == hashlib.sha256(body).hexdigest()


def test_a_ref_that_names_absent_blobs_or_another_archive_is_refused(tmp_path: Path) -> None:
    ref, blobs = split(archive(_stage(tmp_path)))
    store = MemoryInputStore()
    first = sorted(blobs)[0]
    store.put(first, blobs[first])
    with pytest.raises(MissingInputsError) as missing:
        assemble(ref, store.get)
    assert missing.value.digests == sorted(set(blobs) - {first})
    for digest, data in blobs.items():
        store.put(digest, data)
    wrong = ref.model_copy(update={"archive_sha256": "0" * 64})
    with pytest.raises(InputStoreError, match="not the laptop's"):
        assemble(wrong, store.get)
    sneaky = ref.model_copy(update={"inline": {**ref.inline, "blinded/c.parquet": "QQ=="}})
    with pytest.raises(InputStoreError, match="inline"):
        assemble(sneaky, store.get)


def test_the_store_refuses_a_blob_not_named_by_its_sha_and_past_its_limit() -> None:
    store = MemoryInputStore(max_bytes=100)
    with pytest.raises(InputStoreError, match="not its name"):
        store.put("0" * 64, b"data")
    store.put(sha256(b"x" * 80), b"x" * 80)
    with pytest.raises(InputStoreError, match="100 bytes"):
        store.put(sha256(b"y" * 30), b"y" * 30)


def test_drop_overwrites_the_held_bytes_and_leaves_nothing() -> None:
    store = MemoryInputStore()
    data = b"MERIDIAN-CANARY-" + b"7" * 32
    store.put(sha256(data), data)
    held = store._blobs[sha256(data)]  # the buffer itself, to see it zeroed
    assert store.stats().digests == (sha256(data),)
    assert store.drop() == 1
    assert bytes(held) == bytes(len(data))
    stats = store.stats()
    assert (stats.blobs, stats.bytes, stats.digests, stats.dropped) == (0, 0, (), 1)
    assert store.missing([sha256(data)]) == {sha256(data)}


def test_an_idle_store_drops_itself() -> None:
    now = [0.0]
    store = MemoryInputStore(idle_seconds=60, clock=lambda: now[0])
    store.put(sha256(b"a"), b"a")
    now[0] = 59
    store.check_idle()
    assert store.stats().blobs == 1
    now[0] = 59 + 61
    store.check_idle()
    assert store.stats().blobs == 0
    assert store.stats().dropped == 1


def test_a_pass_sends_each_file_once_and_drops_them_at_the_end(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, dispatcher):
        executor = _executor(roots, fake, address)
        client = executor._dispatcher
        agents = ["rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.kri_monitor"]
        results = [executor.run(a, cut_trigger(seq=7 + i), "DC-TINY") for i, a in enumerate(agents)]
        held = dispatcher.inputs.stats()
        # The positive control: while the pass runs, the store holds the cut's files.
        parquet = sorted((roots / "curated" / STUDY / "DC-TINY" / "blinded").glob("*.parquet"))
        assert parquet
        assert {sha256(p.read_bytes()) for p in parquet} <= set(held.digests)
        # Each distinct file went up once: what the store holds is all the blobs sent.
        assert client.blob_bytes == held.bytes == held.received_bytes
        assert client.sent_bytes < held.bytes + 30_000  # three runs: refs only after the first
        executor.drain()
        after = dispatcher.inputs.stats()
        assert (after.blobs, after.bytes, after.digests) == (0, 0, ())
        assert executor.last_drop is not None
        assert executor.last_drop[0] == held.blobs
    registered = build_registry(AGENTS).get("rbqm.kri_monitor")
    reader = ParquetStudyReader(roots / "curated" / STUDY, "DC-TINY")
    expected = drafts_document(
        registered, run_agent(registered, cut_trigger(seq=7), reader, structlog.get_logger())
    )
    assert drafts_document(registered, results[0].drafts) == expected
    assert fake.live == {}


def test_a_store_dropped_mid_pass_is_refilled_once(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, dispatcher):
        executor = _executor(roots, fake, address)
        executor.run("rbqm.kri_monitor", cut_trigger(), "DC-TINY")
        dispatcher.inputs.drop()  # the idle backstop, or a restarted dispatcher
        result = executor.run("rbqm.kri_monitor", cut_trigger(seq=8), "DC-TINY")
        assert result.drafts is not None
        assert dispatcher.inputs.stats().blobs > 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/inputs"),
        ("DELETE", "/v1/inputs"),
        ("POST", "/v1/inputs/missing"),
        ("PUT", "/v1/inputs/" + "a" * 64),
    ],
)
def test_the_input_endpoints_need_the_token(method: str, path: str) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, dispatcher):
        response = httpx.request(method, f"http://{address}{path}", content=b"x" * 10)
        assert response.status_code == 401
        assert dispatcher.inputs.stats().blobs == 0
    assert TOKEN  # the server's token was never sent


def test_a_run_ref_is_refused_when_its_header_names_another_archive(roots: Path) -> None:
    fake = FakeSubstrate(templates={})
    with _server(fake) as (address, _):
        ref = RunRef(members=(), inline={}, archive_sha256="1" * 64)
        nonce = dispatch.new_nonce()
        response = httpx.post(
            f"http://{address}/v1/runs",
            content=ref.model_dump_json().encode(),
            headers={
                dispatch.AUTH_HEADER: f"Bearer {TOKEN}",
                dispatch.NONCE_HEADER: nonce,
                dispatch.ACTOR_HEADER: "a",
                dispatch.TEMPLATE_HEADER: "t",
                "X-Meridian-Agent": "rbqm.kri_monitor",
                "X-Meridian-Archive-Sha256": "2" * 64,
            },
        )
        assert response.status_code == 400
    assert fake.created == []

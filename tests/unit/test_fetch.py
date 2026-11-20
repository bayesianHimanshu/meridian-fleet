import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from meridian.data.fetch import MANIFEST_NAME, FetchError, fetch
from meridian.platform.clock import FixedClock
from tests.support import T0

BASE = "https://example.test/sdtm"
CONTENT = {"dm.xpt": b"dm-bytes", "ae.xpt": b"ae-bytes-longer"}


def _client(calls: list[str], content: dict[str, bytes] = CONTENT) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[1]
        if name not in content:
            return httpx.Response(404)
        return httpx.Response(200, content=content[name])

    return httpx.Client(transport=httpx.MockTransport(handler))


Run = Callable[..., object]


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    return tmp_path / "raw"


def test_downloads_and_writes_manifest(raw: Path) -> None:
    calls: list[str] = []
    report = fetch(
        client=_client(calls),
        clock=FixedClock(T0),
        base_url=BASE,
        domains=["dm", "ae"],
        raw_dir=raw,
    )
    assert report.downloaded == ["dm.xpt", "ae.xpt"]
    assert (raw / "dm.xpt").read_bytes() == b"dm-bytes"
    manifest = json.loads((raw / MANIFEST_NAME).read_text())
    entry = manifest["files"]["ae.xpt"]
    assert entry["url"] == f"{BASE}/ae.xpt"
    assert entry["size_bytes"] == len(b"ae-bytes-longer")
    assert entry["fetched_at"] == "2026-11-07T09:00:00.000000Z"
    assert len(entry["sha256"]) == 64


def test_rerun_is_a_no_op_without_network(raw: Path) -> None:
    clock = FixedClock(T0)
    fetch(client=_client([]), clock=clock, base_url=BASE, domains=["dm", "ae"], raw_dir=raw)
    before = (raw / MANIFEST_NAME).read_bytes()
    clock.advance(timedelta(days=1))
    calls: list[str] = []
    report = fetch(
        client=_client(calls), clock=clock, base_url=BASE, domains=["dm", "ae"], raw_dir=raw
    )
    assert calls == []
    assert report.downloaded == []
    assert report.unchanged == ["dm.xpt", "ae.xpt"]
    assert (raw / MANIFEST_NAME).read_bytes() == before


def test_hash_mismatch_on_existing_file_is_an_error(raw: Path) -> None:
    fetch(client=_client([]), clock=FixedClock(T0), base_url=BASE, domains=["dm"], raw_dir=raw)
    (raw / "dm.xpt").write_bytes(b"tampered")
    with pytest.raises(FetchError, match="refusing to overwrite"):
        fetch(client=_client([]), clock=FixedClock(T0), base_url=BASE, domains=["dm"], raw_dir=raw)
    assert (raw / "dm.xpt").read_bytes() == b"tampered"


def test_unrecorded_file_that_differs_from_remote_is_an_error(raw: Path) -> None:
    raw.mkdir()
    (raw / "dm.xpt").write_bytes(b"something else")
    with pytest.raises(FetchError, match="refusing to overwrite"):
        fetch(client=_client([]), clock=FixedClock(T0), base_url=BASE, domains=["dm"], raw_dir=raw)
    assert (raw / "dm.xpt").read_bytes() == b"something else"
    assert sorted(p.name for p in raw.iterdir()) == ["dm.xpt"]


def test_unrecorded_file_that_matches_remote_is_recorded(raw: Path) -> None:
    raw.mkdir()
    (raw / "dm.xpt").write_bytes(b"dm-bytes")
    report = fetch(
        client=_client([]), clock=FixedClock(T0), base_url=BASE, domains=["dm"], raw_dir=raw
    )
    assert report.downloaded == ["dm.xpt"]
    assert "dm.xpt" in json.loads((raw / MANIFEST_NAME).read_text())["files"]


def test_manifest_only_gains_entries(raw: Path) -> None:
    fetch(client=_client([]), clock=FixedClock(T0), base_url=BASE, domains=["dm"], raw_dir=raw)
    first = json.loads((raw / MANIFEST_NAME).read_text())["files"]["dm.xpt"]
    fetch(
        client=_client([]),
        clock=FixedClock(T0 + timedelta(hours=1)),
        base_url=BASE,
        domains=["dm", "ae"],
        raw_dir=raw,
    )
    files = json.loads((raw / MANIFEST_NAME).read_text())["files"]
    assert files["dm.xpt"] == first
    assert files["ae.xpt"]["fetched_at"] == "2026-11-07T10:00:00.000000Z"


def test_http_error_leaves_no_partial_file(raw: Path) -> None:
    with pytest.raises(FetchError, match="404"):
        fetch(
            client=_client([]),
            clock=FixedClock(T0),
            base_url=BASE,
            domains=["dm", "zz"],
            raw_dir=raw,
        )
    assert sorted(p.name for p in raw.iterdir()) == ["dm.xpt"]

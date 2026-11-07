"""inputs_manifest.json: written by staging, enforced by meridian-worker."""

import hashlib
import json
from pathlib import Path

import pytest

from meridian import worker
from meridian.data.inputs_manifest import (
    MANIFEST_FILE,
    InputsManifest,
    InputsManifestError,
    build_manifest,
    verify_manifest,
)
from meridian.domain.events import ActorSuspended
from meridian.sandbox.staging import stage_cut
from tests.worker_support import cut_trigger, tiny_cut

CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.toml"


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    return stage_cut(tiny_cut(tmp_path / "curated"), cut_trigger(), tmp_path / "stage")


def _run(stage: Path) -> int:
    return worker.main(
        [
            "run",
            "--agent",
            "rbqm.csm",
            "--trigger",
            str(stage / "trigger.json"),
            "--cut-dir",
            str(stage),
            "--config",
            str(CONFIG),
        ]
    )


def test_staging_writes_a_manifest_of_every_other_file(staged: Path) -> None:
    manifest = InputsManifest.model_validate_json((staged / MANIFEST_FILE).read_text())
    listed = {e.path for e in manifest.files}
    on_disk = {
        p.relative_to(staged).as_posix()
        for p in staged.rglob("*")
        if p.is_file() and p.name != MANIFEST_FILE
    }
    assert listed == on_disk
    assert "trigger.json" in listed
    assert all(path.startswith(("blinded/", "ops/")) for path in listed - {"trigger.json"})


def test_runner_and_worker_agree_on_every_hash(staged: Path) -> None:
    """The manifest the host wrote is the manifest the worker recomputes and accepts."""
    written = (staged / MANIFEST_FILE).read_text()
    assert verify_manifest(staged).dumps() == written
    assert build_manifest(staged).dumps() == written
    for entry in json.loads(written)["files"]:
        data = (staged / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["size"] == len(data)


def test_worker_accepts_the_untouched_stage(
    staged: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(staged) == worker.EXIT_OK
    assert capsys.readouterr().out.startswith('{"agent_id":"rbqm.csm"')


def test_worker_refuses_a_changed_file(staged: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = staged / "ops" / "deviations.parquet"
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same size, different bytes
    target.write_bytes(bytes(data))
    assert _run(staged) == worker.EXIT_DATA
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "staged file changed: ops/deviations.parquet" in captured.err


def test_worker_refuses_a_missing_file(staged: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (staged / "blinded" / "vs.parquet").unlink()
    assert _run(staged) == worker.EXIT_DATA
    assert "missing staged files: ['blinded/vs.parquet']" in capsys.readouterr().err


def test_worker_refuses_an_extra_file(staged: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (staged / "blinded" / "randomization.parquet").write_bytes(b"not listed")
    assert _run(staged) == worker.EXIT_DATA
    assert "unexpected staged files: ['blinded/randomization.parquet']" in (capsys.readouterr().err)


def test_worker_refuses_a_missing_or_broken_manifest(
    staged: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (staged / MANIFEST_FILE).write_text("{")
    assert _run(staged) == worker.EXIT_DATA
    (staged / MANIFEST_FILE).unlink()
    assert _run(staged) == worker.EXIT_DATA
    assert capsys.readouterr().out == ""


def test_verify_refuses_a_symlink(staged: Path) -> None:
    (staged / "ops" / "queries.parquet").unlink()
    (staged / "ops" / "queries.parquet").symlink_to(staged / "ops" / "data_entry.parquet")
    with pytest.raises(InputsManifestError, match="symlink"):
        verify_manifest(staged)


def test_actor_suspended_round_trips_the_manifest_hash() -> None:
    done = ActorSuspended(
        agent_id="rbqm.csm", trigger_seq=3, agent_version="2.0.0", inputs_manifest_sha256="d" * 64
    )
    assert ActorSuspended.model_validate_json(done.model_dump_json()) == done
    legacy = ActorSuspended.model_validate({"agent_id": "rbqm.csm", "trigger_seq": 3})
    assert legacy.inputs_manifest_sha256 is None

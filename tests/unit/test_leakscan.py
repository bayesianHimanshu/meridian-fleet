"""The leak check's scanner (phase 4b step 3): needles as stored and decompressed."""

import gzip
import io
import json
import struct
import sys
from compression import zstd
from pathlib import Path

import pytest

from meridian.sandbox import leakscan
from meridian.sandbox.leakscan import CHUNK, SPARSE_MAGIC, Report, scan_tree

CANARY = b"MERIDIAN-CANARY-" + b"0" * 32
OTHER = b"MERIDIAN-PC-" + b"1" * 32


def _sparse(payload: bytes) -> bytes:
    """Substrate's sparse snapshot layout: magic, u32 version, one zstd stream."""
    return SPARSE_MAGIC + struct.pack("<I", 1) + zstd.compress(payload)


@pytest.mark.parametrize(
    ("label", "blob", "where"),
    [
        ("raw", b"x" * 100 + CANARY + b"y" * 100, "o"),
        ("gzip", gzip.compress(b"x" * 100 + CANARY), "o (decompressed)"),
        ("zstd", zstd.compress(b"\0" * 5_000_000 + CANARY), "o (decompressed)"),
        ("sparse", _sparse(b"\0" * 3_000_000 + CANARY + b"\0" * 10), "o (decompressed)"),
        ("two zstd frames", zstd.compress(b"a" * 10) + zstd.compress(CANARY), "o (decompressed)"),
    ],
)
def test_a_needle_is_found_as_stored_or_decompressed(label: str, blob: bytes, where: str) -> None:
    report = Report("t")
    report.scan_bytes("o", blob, [CANARY, OTHER])
    assert report.hits == {0: [where]}, label
    assert report.decompressed == (0 if label == "raw" else 1)


def test_a_needle_across_a_chunk_boundary_is_found() -> None:
    blob = b"x" * (CHUNK - 10) + CANARY + b"y" * 10
    report = Report("t")
    report.scan_bytes("o", blob, [CANARY])
    assert report.hits == {0: ["o"]}
    big = zstd.compress(b"\0" * (leakscan.OUT_LIMIT - 7) + CANARY)  # across output steps
    report.scan_bytes("big", big, [CANARY])
    assert report.hits[0] == ["o", "big (decompressed)"]


def test_a_corrupt_compressed_blob_is_an_error_not_a_pass() -> None:
    report = Report("t")
    report.scan_bytes("bad.zst", b"\x28\xb5\x2f\xfd" + b"garbage" * 10, [CANARY])
    assert report.hits == {}
    assert report.errors
    assert "bad.zst" in report.errors[0]


def test_scan_tree_reads_files_skips_excluded_dirs_and_does_not_follow_links(
    tmp_path: Path,
) -> None:
    (tmp_path / "var" / "lib").mkdir(parents=True)
    (tmp_path / "var" / "lib" / "layer.gz").write_bytes(gzip.compress(CANARY))
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "mem").write_bytes(OTHER)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret").write_bytes(OTHER)
    (tmp_path / "link").symlink_to(outside)
    (tmp_path / "file-link").symlink_to(outside / "secret")
    report = scan_tree(tmp_path, [CANARY, OTHER], excludes=("proc",))
    assert report.hits == {0: [f"{tmp_path}/var/lib/layer.gz (decompressed)"]}
    assert report.items == 1


def test_the_pod_entry_point_reads_needles_from_stdin_and_never_prints_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a").write_bytes(b"..." + OTHER)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{CANARY.decode()}\n{OTHER.decode()}\n"))
    assert leakscan.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert CANARY.decode() not in out
    assert OTHER.decode() not in out
    report = json.loads(out)
    assert report["hits"] == {"1": [f"{tmp_path}/a"]}
    assert report["items"] == 1

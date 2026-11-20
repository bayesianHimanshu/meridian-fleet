"""Search bytes for needles, decompressing as it goes (phase 4b step 3, the leak check).

One scanner for every store the leak check reads: an object of the snapshot bucket (in
memory), a file of a node's filesystem (streamed in chunks, so a multi-GB containerd blob
never sits in memory) and a log. A blob is searched as stored and, if it is compressed,
decompressed: Substrate's sparse snapshot format (magic ATESPRSE, a little-endian u32
version, then one zstd stream; pkg/objectstorage/sparsezstd.go, the decoder of phase 4a),
plain zstd, and gzip. Chunks overlap by the longest needle, so a needle across a chunk
boundary is still found.

Run inside a scan pod on a node (meridian.sandbox.leakcheck):

    python -m meridian.sandbox.leakscan --root /host < needles

reads one needle per line from stdin (never from argv, so a needle is in no process list,
pod spec or log), walks the root without following links or leaving it for /proc, /sys,
/dev or /run, and prints one JSON report naming needles by their index only.
"""

import argparse
import itertools
import json
import os
import stat
import sys
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from compression import zstd
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPARSE_MAGIC = b"ATESPRSE"
SPARSE_HEADER = len(SPARSE_MAGIC) + 4
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
CHUNK = 4 * 1024 * 1024
OUT_LIMIT = 16 * 1024 * 1024  # most bytes one decompression step may produce
NODE_EXCLUDES = ("proc", "sys", "dev", "run")


def kind_of(head: bytes) -> str | None:
    if head.startswith(SPARSE_MAGIC):
        return "sparse"
    if head.startswith(ZSTD_MAGIC):
        return "zstd"
    if head.startswith(GZIP_MAGIC):
        return "gzip"
    return None


def _zstd_stream(chunks: Iterable[bytes]) -> Iterator[bytes]:
    d = zstd.ZstdDecompressor()
    for chunk in chunks:
        data = chunk
        while True:
            if d.eof:  # the frame ended: what follows is a further frame
                data = d.unused_data + data
                d = zstd.ZstdDecompressor()
                if not data:
                    break
            out = d.decompress(data, max_length=OUT_LIMIT)
            data = b""
            if out:
                yield out
            if not d.eof and d.needs_input:
                break


def _gzip_stream(chunks: Iterable[bytes]) -> Iterator[bytes]:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for chunk in chunks:
        data = chunk
        while data:
            out = d.decompress(data, OUT_LIMIT)
            if out:
                yield out
            data = d.unconsumed_tail
            if d.eof:
                return


def _skip(chunks: Iterable[bytes], n: int) -> Iterator[bytes]:
    for chunk in chunks:
        if n >= len(chunk):
            n -= len(chunk)
            continue
        yield chunk[n:]
        n = 0


def decompressed(kind: str, chunks: Iterable[bytes]) -> Iterator[bytes]:
    if kind == "sparse":
        return _zstd_stream(_skip(chunks, SPARSE_HEADER))
    if kind == "zstd":
        return _zstd_stream(chunks)
    return _gzip_stream(chunks)


@dataclass(slots=True)
class Found:
    """Which needles (by index) a stream holds, and how many bytes were searched."""

    needles: set[int] = field(default_factory=set)
    scanned: int = 0


def search(stream: Iterable[bytes], needles: Sequence[bytes]) -> Found:
    found = Found()
    keep = max((len(n) for n in needles), default=1) - 1
    tail = b""
    for chunk in stream:
        window = tail + chunk
        found.scanned += len(chunk)
        for i, needle in enumerate(needles):
            if i not in found.needles and needle in window:
                found.needles.add(i)
        tail = window[-keep:] if keep else b""
    return found


@dataclass(slots=True)
class Report:
    """A store's search: items and bytes read, and per needle (by index) where it was."""

    store: str
    items: int = 0
    bytes_scanned: int = 0
    decompressed: int = 0
    hits: dict[int, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def scan(
        self, name: str, opener: Callable[[], Iterable[bytes]], needles: Sequence[bytes]
    ) -> None:
        """Search one blob as stored and, if compressed, decompressed. opener() gives its
        chunks from the start (called twice for a compressed blob)."""
        self.items += 1
        first = iter(opener())
        head = next(first, b"")
        raw = search(itertools.chain([head], first), needles) if head else Found()
        self.bytes_scanned += raw.scanned
        for i in raw.needles:
            self.hits.setdefault(i, []).append(name)
        kind = kind_of(head)
        if kind is None:
            return
        try:
            unpacked = search(decompressed(kind, opener()), needles)
        except (zstd.ZstdError, zlib.error, EOFError) as exc:
            self.errors.append(f"{name}: {kind}: {exc}"[:300])
            return
        self.decompressed += 1
        self.bytes_scanned += unpacked.scanned
        for i in unpacked.needles - raw.needles:
            self.hits.setdefault(i, []).append(f"{name} (decompressed)")

    def scan_bytes(self, name: str, data: bytes, needles: Sequence[bytes]) -> None:
        self.scan(name, lambda: (data[i : i + CHUNK] for i in range(0, len(data), CHUNK)), needles)

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "items": self.items,
            "bytes_scanned": self.bytes_scanned,
            "decompressed": self.decompressed,
            "hits": {str(k): v[:50] for k, v in sorted(self.hits.items())},
            "errors": self.errors[:50],
        }


def _file_chunks(path: Path) -> Callable[[], Iterator[bytes]]:
    def chunks() -> Iterator[bytes]:
        with path.open("rb") as handle:
            while block := handle.read(CHUNK):
                yield block

    return chunks


def scan_tree(root: Path, needles: Sequence[bytes], excludes: Sequence[str] = ()) -> Report:
    """Every regular file under root (no symlinks followed, the excluded top-level
    directories skipped), each as stored and decompressed."""
    report = Report(f"filesystem {root}")
    skip = {str(root / e) for e in excludes}
    for dirpath, dirs, files in os.walk(root, onerror=lambda e: report.errors.append(str(e))):
        dirs[:] = [d for d in dirs if os.path.join(dirpath, d) not in skip]
        for name in files:
            path = Path(dirpath) / name
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                report.scan(path.as_posix(), _file_chunks(path), needles)
            except OSError as exc:
                report.errors.append(f"{path}: {exc}"[:300])
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meridian.sandbox.leakscan")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--exclude", default=",".join(NODE_EXCLUDES))
    args = parser.parse_args(argv)
    needles = [line.strip().encode() for line in sys.stdin if line.strip()]
    if not needles:
        print("no needles on stdin", file=sys.stderr)
        return 2
    excludes = [e for e in args.exclude.split(",") if e]
    report = scan_tree(args.root, needles, excludes)
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

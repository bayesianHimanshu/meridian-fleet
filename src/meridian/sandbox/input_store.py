"""Inputs staged once per pass, by content (phase 4b step 4, docs/input-staging.md).

A run's archive is its meta/ (trigger.json and inputs_manifest.json, about 1.8 KB, new
for every run) and the zone's parquet files, which repeat from run to run and from cut to
cut: a full pass of 910 runs uploads 814 MB as whole archives but holds only 731 distinct
files, 12.3 MB. So the laptop sends each distinct file once, keyed by its sha256, into an
InputStore in the dispatcher's memory, and each run sends only a RunRef: the archive's
member list (path, size, sha256) and its meta/ files inline. The dispatcher rebuilds the
archive byte for byte (archive_from: sorted, mode 0644, owner 0, mtime 0), checks that its
sha256 is the one the laptop computed, and sends it to the actor as before; the actor's
own checks (archive sha256, inputs manifest) are unchanged.

What rests where, and for how long (the point of this module):
- MemoryInputStore holds bytes in the dispatcher process only: never on disk (the pod's
  root filesystem is read-only and nothing here writes /tmp), never logged.
- The laptop drops the store at the end of every pass (DispatcherExecutor.drain), and the
  store drops itself when idle for idle_seconds (a backstop if the laptop never comes
  back). drop() overwrites every held buffer with zeros before releasing it.
- It refuses a blob whose sha256 is not its name, and anything past max_bytes.
- stats() names digests and sizes, never contents, so the leak check can show a planted
  file was held (its positive control) and is gone after the drop.
"""

import base64
import hashlib
import io
import tarfile
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

META_PREFIX = "meta/"
SHA256 = r"^[0-9a-f]{64}$"


class InputStoreError(Exception):
    pass


class MissingInputsError(InputStoreError):
    """A RunRef names blobs the store does not hold (never sent, or dropped since)."""

    def __init__(self, digests: Collection[str]) -> None:
        self.digests = sorted(digests)
        super().__init__(f"{len(self.digests)} input blob(s) not held")


@dataclass(frozen=True, slots=True)
class StoreStats:
    blobs: int
    bytes: int
    digests: tuple[str, ...]
    dropped: int = 0  # drops so far (by the laptop or by the idle backstop)
    received_bytes: int = 0  # blob bytes put since the process started


class InputStore(Protocol):
    """Content-addressed inputs for the runs of one pass."""

    def missing(self, digests: Collection[str]) -> set[str]: ...

    def put(self, digest: str, data: bytes) -> None: ...

    def stats(self) -> StoreStats: ...

    def drop(self) -> int:
        """Release every blob; returns how many were held."""
        ...


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class MemoryInputStore:
    """The dispatcher's store: bytes in this process's memory for one pass at most."""

    max_bytes: int = 256 * 1024 * 1024
    idle_seconds: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _blobs: dict[str, bytearray] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last: float = 0.0
    _dropped: int = 0
    _received: int = 0

    def _touch(self) -> None:
        """Called under the lock: the idle backstop, then mark this use."""
        now = self.clock()
        if self._blobs and now - self._last > self.idle_seconds:
            self._drop_locked()
        self._last = now

    def _drop_locked(self) -> int:
        held = len(self._blobs)
        for buffer in self._blobs.values():
            buffer[:] = bytes(len(buffer))  # zeros over the bytes, then release
        self._blobs.clear()
        if held:
            self._dropped += 1
        return held

    def missing(self, digests: Collection[str]) -> set[str]:
        with self._lock:
            self._touch()
            return {d for d in digests if d not in self._blobs}

    def put(self, digest: str, data: bytes) -> None:
        if sha256(data) != digest:
            raise InputStoreError("a blob's sha256 is not its name; refused")
        with self._lock:
            self._touch()
            if digest in self._blobs:
                return
            held = sum(len(b) for b in self._blobs.values())
            if held + len(data) > self.max_bytes:
                raise InputStoreError(f"the input store would pass {self.max_bytes} bytes")
            self._blobs[digest] = bytearray(data)
            self._received += len(data)

    def get(self, digest: str) -> bytes:
        with self._lock:
            self._touch()
            buffer = self._blobs.get(digest)
            if buffer is None:
                raise MissingInputsError([digest])
            return bytes(buffer)

    def check_idle(self) -> None:
        """The backstop, from a timer: drop if nothing used the store for idle_seconds."""
        with self._lock:
            if self._blobs and self.clock() - self._last > self.idle_seconds:
                self._drop_locked()

    def stats(self) -> StoreStats:
        with self._lock:
            return StoreStats(
                blobs=len(self._blobs),
                bytes=sum(len(b) for b in self._blobs.values()),
                digests=tuple(sorted(self._blobs)),
                dropped=self._dropped,
                received_bytes=self._received,
            )

    def drop(self) -> int:
        with self._lock:
            return self._drop_locked()


# ------------------------------------------------------------------ archives and refs


def archive_from(entries: Iterable[tuple[str, bytes]]) -> bytes:
    """An uncompressed tar of the given (path, bytes), byte for byte the same for the same
    files: sorted by path, mode 0644, owner 0, mtime 0, no folder entries."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(entries, key=lambda e: e[0]):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def archive_entries(body: bytes) -> list[tuple[str, bytes]]:
    """The (path, bytes) of an archive, in its order."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
        for member in tar:
            if not member.isfile():
                raise InputStoreError(f"unexpected archive member {member.name}")
            handle = tar.extractfile(member)
            assert handle is not None
            out.append((member.name, handle.read()))
    return out


class Member(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256)


class RunRef(BaseModel):
    """A run's archive by reference: data members by sha256, meta/ inline (base64)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    members: tuple[Member, ...]
    inline: dict[str, str]
    archive_sha256: str = Field(pattern=SHA256)


def split(body: bytes) -> tuple[RunRef, dict[str, bytes]]:
    """A run's archive as a RunRef and the blobs it names (sha256 to bytes)."""
    members, inline, blobs = [], {}, {}
    for name, data in archive_entries(body):
        if name.startswith(META_PREFIX):
            inline[name] = base64.b64encode(data).decode("ascii")
            continue
        digest = sha256(data)
        members.append(Member(path=name, size=len(data), sha256=digest))
        blobs[digest] = data
    return RunRef(members=tuple(members), inline=inline, archive_sha256=sha256(body)), blobs


def assemble(ref: RunRef, get: Callable[[str], bytes]) -> bytes:
    """Rebuild the archive a RunRef names; raise unless it is the laptop's, byte for byte."""
    absent = {m.sha256 for m in ref.members}
    entries: list[tuple[str, bytes]] = []
    for m in ref.members:
        try:
            data = get(m.sha256)
        except MissingInputsError:
            continue
        absent.discard(m.sha256)
        if len(data) != m.size:
            raise InputStoreError(f"{m.path}: held blob has the wrong size")
        entries.append((m.path, data))
    missing = {m.sha256 for m in ref.members if m.sha256 in absent}
    if missing:
        raise MissingInputsError(missing)
    for name, text in ref.inline.items():
        if not name.startswith(META_PREFIX):
            raise InputStoreError(f"only {META_PREFIX} goes inline, not {name}")
        entries.append((name, base64.b64decode(text, validate=True)))
    body = archive_from(entries)
    if sha256(body) != ref.archive_sha256:
        raise InputStoreError("the rebuilt archive is not the laptop's (sha256 differs)")
    return body


class Uploader:
    """The laptop's side of one pass: sends each blob the store lacks, once. Runs that
    need a blob another run is already sending wait for it instead of sending it again;
    digests known to be held are not asked about again until forget()."""

    def __init__(self, store: InputStore, wait_seconds: float = 600.0) -> None:
        self._store = store
        self._wait = wait_seconds
        self._known: set[str] = set()
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def ensure(self, blobs: Mapping[str, bytes]) -> int:
        """Make the store hold every blob; returns the bytes this call sent."""
        with self._lock:
            mine = [d for d in blobs if d not in self._known and d not in self._inflight]
            theirs = [self._inflight[d] for d in blobs if d in self._inflight]
            for d in mine:
                self._inflight[d] = threading.Event()
        sent = 0
        try:
            if mine:
                for digest in sorted(self._store.missing(mine)):
                    self._store.put(digest, blobs[digest])
                    sent += len(blobs[digest])
                with self._lock:
                    self._known.update(mine)
        finally:
            with self._lock:
                for d in mine:
                    self._inflight.pop(d).set()
        for event in theirs:
            event.wait(self._wait)  # if that upload failed, the run's 409 sends it again
        return sent

    def forget(self, digests: Collection[str] | None = None) -> None:
        with self._lock:
            if digests is None:
                self._known.clear()
            else:
                self._known.difference_update(digests)

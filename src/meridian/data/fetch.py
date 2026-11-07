"""Download the CDISC pilot SDTM transport files, append-only and hash-checked."""

import os
from collections.abc import Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from meridian.data.files import sha256_file, write_bytes_atomic
from meridian.domain.ids import UtcDatetime
from meridian.ledger.hashing import canonical_json
from meridian.platform.clock import Clock

MANIFEST_NAME = "fetch_manifest.json"
_CHUNK = 1 << 16


class FetchError(Exception):
    """Raised when raw data cannot be fetched or would be overwritten."""


class FetchedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    sha256: str
    size_bytes: int
    fetched_at: UtcDatetime


class FetchManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files: dict[str, FetchedFile]

    @classmethod
    def load(cls, raw_dir: Path) -> FetchManifest:
        path = raw_dir / MANIFEST_NAME
        if not path.exists():
            return cls(files={})
        return cls.model_validate_json(path.read_bytes())

    def dumps(self) -> str:
        return canonical_json(self.model_dump(mode="python")) + "\n"


class FetchReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    downloaded: list[str]
    unchanged: list[str]
    manifest: FetchManifest


def file_name(domain: str) -> str:
    return f"{domain}.xpt"


def _download(client: httpx.Client, url: str, target: Path) -> None:
    """Stream to a sibling temp file and rename into place, so no partial file is left."""
    tmp = target.with_name(f".{target.name}.part-{os.getpid()}")
    try:
        with client.stream("GET", url) as response:
            if response.status_code != httpx.codes.OK:
                raise FetchError(f"GET {url} returned HTTP {response.status_code}")
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        tmp.rename(target)
    finally:
        tmp.unlink(missing_ok=True)


def fetch(
    *,
    client: httpx.Client,
    clock: Clock,
    base_url: str,
    domains: Sequence[str],
    raw_dir: Path,
) -> FetchReport:
    """Fetch each domain file once.

    A file already recorded in the manifest is re-hashed and must still match; a
    mismatch is an error and nothing is overwritten. A file present on disk but not in
    the manifest is downloaded again to a temp file and must match byte for byte.
    The manifest only ever gains entries; existing entries are never changed.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = FetchManifest.load(raw_dir)
    entries = dict(manifest.files)
    downloaded: list[str] = []
    unchanged: list[str] = []

    for domain in domains:
        name = file_name(domain)
        target = raw_dir / name
        url = f"{base_url.rstrip('/')}/{name}"
        recorded = entries.get(name)

        if recorded is not None:
            if not target.exists():
                raise FetchError(f"{name} is in {MANIFEST_NAME} but missing on disk")
            actual = sha256_file(target)
            if actual != recorded.sha256:
                raise FetchError(
                    f"{name} on disk has sha256 {actual}, manifest says {recorded.sha256}; "
                    "refusing to overwrite raw data"
                )
            unchanged.append(name)
            continue

        if target.exists():
            scratch = target.with_name(f".{name}.verify-{os.getpid()}")
            try:
                _download(client, url, scratch)
                if sha256_file(scratch) != sha256_file(target):
                    raise FetchError(
                        f"{name} exists on disk, is not in {MANIFEST_NAME}, and differs from "
                        f"{url}; refusing to overwrite raw data"
                    )
            finally:
                scratch.unlink(missing_ok=True)
        else:
            _download(client, url, target)
        entries[name] = FetchedFile(
            url=url,
            sha256=sha256_file(target),
            size_bytes=target.stat().st_size,
            fetched_at=clock.now(),
        )
        downloaded.append(name)

    result = FetchManifest(files=entries)
    if downloaded:
        write_bytes_atomic(raw_dir / MANIFEST_NAME, result.dumps().encode("utf-8"))
    return FetchReport(downloaded=downloaded, unchanged=unchanged, manifest=result)

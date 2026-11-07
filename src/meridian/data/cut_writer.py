"""Write a curated cut atomically and deterministically.

Layout: <curated_root>/<study_id>/<cut_id>/{blinded,ops,unblinded}/*.parquet + manifest.json.
Everything is written into a hidden temp folder next to the target and renamed into
place at the end, so a crash never leaves a partial cut under the final name.
A cut that already exists is never rewritten: identical file hashes make the write a
no-op, anything else is an error.
"""

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

from meridian.data.blinding import assert_blinded
from meridian.data.files import sha256_file
from meridian.domain.ids import UtcDatetime

MANIFEST_NAME = "manifest.json"
BLINDED_AREA = "blinded"
OPS_AREA = "ops"
UNBLINDED_AREA = "unblinded"

PARQUET_COMPRESSION: Literal["zstd"] = "zstd"
PARQUET_COMPRESSION_LEVEL = 3
PARQUET_ROW_GROUP_SIZE = 65_536

ParquetWriter = Callable[[pl.DataFrame, Path], None]


class CutExistsError(Exception):
    """Raised when a cut already exists with different content."""


class ManifestFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str
    rows: int
    columns: tuple[str, ...]


class SourceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str
    sha256: str


class CutManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cut_id: str
    study_id: str
    created_at: UtcDatetime
    seed: int
    seed_record: dict[str, Any]
    files: tuple[ManifestFile, ...]
    sources: tuple[SourceFile, ...]
    # Set only on a cut derived from another cut; its lineage is that cut and nothing else.
    derived_from_cut: str | None = None
    derived_from_manifest_sha256: str | None = None

    def file_hashes(self) -> dict[str, str]:
        return {f.path: f.sha256 for f in self.files}

    @classmethod
    def load(cls, cut_dir: Path) -> CutManifest:
        return cls.model_validate_json((cut_dir / MANIFEST_NAME).read_bytes())


@dataclass(frozen=True, slots=True)
class CutWriteResult:
    path: Path
    manifest: CutManifest
    created: bool


def render_manifest(manifest: CutManifest) -> str:
    """Stable JSON. Lineage fields are omitted when unset, so base cuts keep their shape."""
    data = manifest.model_dump(mode="json", exclude_none=False)
    for key in ("derived_from_cut", "derived_from_manifest_sha256"):
        if data[key] is None:
            del data[key]
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def write_parquet_deterministic(frame: pl.DataFrame, path: Path) -> None:
    """Fixed compression and row group size; polars writes no timestamps in metadata."""
    frame.write_parquet(
        path,
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
        row_group_size=PARQUET_ROW_GROUP_SIZE,
        statistics=True,
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_cut(
    *,
    curated_root: Path,
    study_id: str,
    cut_id: str,
    blinded: Mapping[str, pl.DataFrame],
    ops: Mapping[str, pl.DataFrame],
    unblinded: Mapping[str, pl.DataFrame],
    created_at: UtcDatetime,
    seed: int,
    seed_record: dict[str, Any],
    sources: Sequence[SourceFile],
    writer: ParquetWriter = write_parquet_deterministic,
    derived_from_cut: str | None = None,
    derived_from_manifest_sha256: str | None = None,
) -> CutWriteResult:
    # Blinding is checked before a single byte is written.
    for area, tables in ((BLINDED_AREA, blinded), (OPS_AREA, ops)):
        for name, frame in tables.items():
            assert_blinded(f"{area}/{name}", frame)

    study_dir = curated_root / study_id
    target = study_dir / cut_id
    study_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{cut_id}.tmp-", dir=study_dir))
    try:
        files: list[ManifestFile] = []
        for area, tables in (
            (BLINDED_AREA, blinded),
            (OPS_AREA, ops),
            (UNBLINDED_AREA, unblinded),
        ):
            (tmp / area).mkdir()
            for name in sorted(tables):
                frame = tables[name]
                relative = f"{area}/{name}.parquet"
                writer(frame, tmp / relative)
                files.append(
                    ManifestFile(
                        path=relative,
                        sha256=sha256_file(tmp / relative),
                        rows=frame.height,
                        columns=tuple(frame.columns),
                    )
                )
        manifest = CutManifest(
            cut_id=cut_id,
            study_id=study_id,
            created_at=created_at,
            seed=seed,
            seed_record=seed_record,
            files=tuple(files),
            sources=tuple(sorted(sources, key=lambda s: s.name)),
            derived_from_cut=derived_from_cut,
            derived_from_manifest_sha256=derived_from_manifest_sha256,
        )
        text = render_manifest(manifest)
        (tmp / MANIFEST_NAME).write_text(text, encoding="utf-8")

        if target.exists():
            existing = CutManifest.load(target)
            if existing.file_hashes() == manifest.file_hashes():
                return CutWriteResult(path=target, manifest=existing, created=False)
            raise CutExistsError(
                f"cut {cut_id} already exists at {target} with different content; "
                "cuts are never rewritten, use a new cut id"
            )
        tmp.rename(target)
        _fsync_dir(study_dir)
        return CutWriteResult(path=target, manifest=manifest, created=True)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)

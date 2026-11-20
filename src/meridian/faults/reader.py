"""Privileged reader of a whole cut, unblinded/ included. Only the fault injector uses it."""

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from meridian.data.cut_writer import (
    BLINDED_AREA,
    MANIFEST_NAME,
    OPS_AREA,
    UNBLINDED_AREA,
    CutManifest,
)
from meridian.data.files import sha256_file


@dataclass(frozen=True, slots=True)
class FullCut:
    manifest: CutManifest
    manifest_sha256: str
    blinded: dict[str, pl.DataFrame]
    ops: dict[str, pl.DataFrame]
    unblinded: dict[str, pl.DataFrame]


class FullCutReader:
    def __init__(self, cut_dir: Path) -> None:
        self._cut_dir = cut_dir

    def read(self) -> FullCut:
        manifest = CutManifest.load(self._cut_dir)
        areas: dict[str, dict[str, pl.DataFrame]] = {
            BLINDED_AREA: {},
            OPS_AREA: {},
            UNBLINDED_AREA: {},
        }
        for entry in manifest.files:
            area, file_name = entry.path.split("/", 1)
            path = self._cut_dir / entry.path
            if sha256_file(path) != entry.sha256:
                raise ValueError(f"{path} does not match its manifest sha256")
            areas[area][file_name.removesuffix(".parquet")] = pl.read_parquet(path)
        return FullCut(
            manifest=manifest,
            manifest_sha256=sha256_file(self._cut_dir / MANIFEST_NAME),
            blinded=areas[BLINDED_AREA],
            ops=areas[OPS_AREA],
            unblinded=areas[UNBLINDED_AREA],
        )

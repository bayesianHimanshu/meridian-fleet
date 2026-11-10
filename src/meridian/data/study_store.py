"""Read access to curated cuts.

ParquetStudyReader is what agents get: it can only see <cut>/blinded and <cut>/ops.
UnblindedReader is a separate class for <cut>/unblinded; nothing wires it in phase 1.
"""

import re
from pathlib import Path
from typing import Protocol

import polars as pl

from meridian.data.cut_writer import (
    BLINDED_AREA,
    OPS_AREA,
    UNBLINDED_AREA,
    CutManifest,
)

_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_CUT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class StudyAccessError(PermissionError):
    """Raised when a table name would resolve outside the reader's allowed areas."""


class MissingTableError(KeyError):
    """No such table in the reader's areas. A KeyError, as before, so callers still work."""


class StudyReader(Protocol):
    @property
    def cut_id(self) -> str: ...

    @property
    def study_id(self) -> str: ...

    def table(self, name: str) -> pl.DataFrame: ...

    def tables(self) -> list[str]: ...


class _AreaReader:
    """Resolves table names strictly inside a fixed set of areas of one cut."""

    _areas: tuple[str, ...] = ()

    def __init__(self, root: Path, cut_id: str) -> None:
        if not _CUT_ID.fullmatch(cut_id):
            raise StudyAccessError(f"invalid cut id {cut_id!r}")
        cut_dir = (root / cut_id).resolve(strict=True)
        if cut_dir.parent != root.resolve(strict=True):
            raise StudyAccessError(f"cut {cut_id!r} resolves outside {root}")
        self._bind(cut_dir, cut_id, CutManifest.load(cut_dir).study_id)

    def _bind(self, cut_dir: Path, cut_id: str, study_id: str) -> None:
        self._cut_dir = cut_dir
        self._cut_id = cut_id
        self._study_id = study_id
        # Built from the resolved cut dir plus literal names, never by resolving the
        # area itself, so a symlinked area that points elsewhere is refused below.
        self._allowed = tuple(self._cut_dir / area for area in self._areas)

    @property
    def cut_id(self) -> str:
        return self._cut_id

    @property
    def study_id(self) -> str:
        return self._study_id

    def _resolve(self, area_dir: Path, name: str) -> Path | None:
        candidate = area_dir / f"{name}.parquet"
        if not candidate.exists():
            return None
        resolved = candidate.resolve(strict=True)
        if resolved.parent != area_dir:
            raise StudyAccessError(f"table {name!r} resolves outside {area_dir.name}/")
        return resolved

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not _TABLE_NAME.fullmatch(name):
            raise StudyAccessError(
                f"invalid table name {name!r}: only lower-case names like 'dm' or 'queries'"
            )
        found = [p for area in self._allowed if (p := self._resolve(area, name)) is not None]
        if not found:
            raise MissingTableError(
                f"no table {name!r} in {', '.join(self._areas)} of cut {self._cut_id}"
            )
        if len(found) > 1:
            raise StudyAccessError(f"table {name!r} is ambiguous across areas")
        return found[0]

    def table(self, name: str) -> pl.DataFrame:
        return pl.read_parquet(self._path(name))

    def tables(self) -> list[str]:
        names: list[str] = []
        for area in self._allowed:
            if not area.is_dir() or area.is_symlink():
                continue
            for entry in sorted(area.glob("*.parquet")):
                name = entry.stem
                if not _TABLE_NAME.fullmatch(name):
                    continue
                try:
                    self._resolve(area, name)
                except StudyAccessError:
                    continue
                names.append(name)
        return names


class ParquetStudyReader(_AreaReader):
    """Blinded and ops tables only. There is no parameter that widens this."""

    _areas = (BLINDED_AREA, OPS_AREA)

    @classmethod
    def at_cut_dir(cls, cut_dir: Path, *, cut_id: str, study_id: str) -> ParquetStudyReader:
        """A reader over a bare cut directory holding only blinded/ and ops/.

        Used inside a sandbox, where the manifest is not staged (it lists the unblinded
        files), so the cut and study ids come from the trigger instead.
        """
        if not _CUT_ID.fullmatch(cut_id):
            raise StudyAccessError(f"invalid cut id {cut_id!r}")
        reader = cls.__new__(cls)
        reader._bind(cut_dir.resolve(strict=True), cut_id, study_id)
        return reader


class UnblindedReader(_AreaReader):
    """Unblinded tables only. Not wired into anything in phase 1."""

    _areas = (UNBLINDED_AREA,)


# Zones whose agents also read the cut's safety database snapshot (its blinded area).
SAFETY_DB_ZONES = frozenset({"safety"})
# Zones whose agents also read the cut's eTMF index (the blinded area of its eTMF
# snapshot; staged into the sandbox as /stage/tmf).
TMF_ZONES = frozenset({"quality"})
TMF_STAGE_AREA = "tmf"


AUDIT_STAGE_AREA = "audit"


class AuditStageReader(_AreaReader):
    """The audit extract under <dir>/audit (a review's own folder on the host, or the
    stage inside a sandbox), nothing else. Its cut id is the ledger scope, not a cut."""

    _areas = (AUDIT_STAGE_AREA,)

    @classmethod
    def at_cut_dir(cls, cut_dir: Path, *, cut_id: str, study_id: str) -> AuditStageReader:
        if not _CUT_ID.fullmatch(cut_id):
            raise StudyAccessError(f"invalid cut id {cut_id!r}")
        reader = cls.__new__(cls)
        reader._bind(cut_dir.resolve(strict=True), cut_id, study_id)
        return reader


class TmfStageReader(_AreaReader):
    """Inside a sandbox: the eTMF tables staged under <cut dir>/tmf, nothing else."""

    _areas = (TMF_STAGE_AREA,)

    @classmethod
    def at_cut_dir(cls, cut_dir: Path, *, cut_id: str, study_id: str) -> TmfStageReader:
        if not _CUT_ID.fullmatch(cut_id):
            raise StudyAccessError(f"invalid cut id {cut_id!r}")
        reader = cls.__new__(cls)
        reader._bind(cut_dir.resolve(strict=True), cut_id, study_id)
        return reader


class LayeredStudyReader:
    """One cut's blinded and ops tables, plus the blinded tables of a second store for the
    same cut (the safety database snapshot). Both readers are blinded; a table name may
    come from only one of them."""

    def __init__(self, primary: StudyReader, extra: StudyReader) -> None:
        if (primary.cut_id, primary.study_id) != (extra.cut_id, extra.study_id):
            raise StudyAccessError(
                f"readers for different cuts: {primary.cut_id} and {extra.cut_id}"
            )
        both = set(primary.tables()) & set(extra.tables())
        if both:
            raise StudyAccessError(f"table names in both stores: {sorted(both)}")
        self._primary = primary
        self._extra = extra
        self._extra_names = frozenset(extra.tables())

    @property
    def cut_id(self) -> str:
        return self._primary.cut_id

    @property
    def study_id(self) -> str:
        return self._primary.study_id

    def table(self, name: str) -> pl.DataFrame:
        if name in self._extra_names:
            return self._extra.table(name)
        return self._primary.table(name)

    def tables(self) -> list[str]:
        return sorted({*self._primary.tables(), *self._extra_names})

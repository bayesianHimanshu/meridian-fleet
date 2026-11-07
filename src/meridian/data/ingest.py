"""Turn fetched raw pilot files into a curated cut and record a DataCut event."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import polars as pl

from meridian.data.cdisc_pilot import SDTM_DOMAINS, curate
from meridian.data.cut_writer import (
    CutManifest,
    ParquetWriter,
    SourceFile,
    write_cut,
    write_parquet_deterministic,
)
from meridian.data.fetch import FetchManifest, file_name
from meridian.data.files import sha256_file
from meridian.data.ops_synth import OpsSynthParams, synthesize
from meridian.data.sites import ensure_site_map
from meridian.data.xpt_reader import read_xpt
from meridian.domain.enums import EventType, Visibility
from meridian.domain.events import DataCut, Event, EventDraft
from meridian.ledger.store import LedgerReader, LedgerWriter
from meridian.platform.clock import Clock

INGEST_ACTOR = "system.ingest"


class IngestError(Exception):
    pass


class Ledger(LedgerReader, LedgerWriter, Protocol):
    pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    cut_path: Path
    manifest: CutManifest
    cut_created: bool
    event: Event | None
    event_appended: bool


class IngestService:
    def __init__(
        self,
        *,
        clock: Clock,
        ledger: Ledger,
        curated_root: Path,
        sites_file: Path,
        study_id: str,
        ops_params: OpsSynthParams,
        writer: ParquetWriter = write_parquet_deterministic,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._curated_root = curated_root
        self._sites_file = sites_file
        self._study_id = study_id
        self._ops_params = ops_params
        self._writer = writer

    def load_raw(
        self, raw_dir: Path, domains: Sequence[str] = SDTM_DOMAINS
    ) -> tuple[dict[str, pl.DataFrame], list[SourceFile]]:
        """Read each raw XPT after checking it still matches the fetch manifest."""
        manifest = FetchManifest.load(raw_dir)
        frames: dict[str, pl.DataFrame] = {}
        sources: list[SourceFile] = []
        for domain in domains:
            name = file_name(domain)
            entry = manifest.files.get(name)
            if entry is None:
                raise IngestError(f"{name} is not in the fetch manifest; run `meridian data fetch`")
            path = raw_dir / name
            if sha256_file(path) != entry.sha256:
                raise IngestError(f"{path} no longer matches its fetch manifest sha256")
            frames[domain] = read_xpt(path)
            sources.append(SourceFile(name=name, url=entry.url, sha256=entry.sha256))
        return frames, sources

    def ingest_frames(
        self,
        frames: Mapping[str, pl.DataFrame],
        *,
        cut_id: str,
        seed: int,
        sources: Sequence[SourceFile],
    ) -> IngestResult:
        dm = frames.get("dm")
        if dm is None:
            raise IngestError("the dm frame is required")
        site_map = ensure_site_map(self._sites_file, dm["SITEID"].to_list())
        curated = curate(frames, site_map, self._study_id)
        ops = synthesize(curated.blinded["dm"], curated.blinded["sv"], seed, self._ops_params)
        written = write_cut(
            curated_root=self._curated_root,
            study_id=self._study_id,
            cut_id=cut_id,
            blinded=curated.blinded,
            ops=ops.tables,
            unblinded=curated.unblinded,
            created_at=self._clock.now(),
            seed=seed,
            seed_record=ops.seed_record.model_dump(mode="json"),
            sources=sources,
            writer=self._writer,
        )
        event, appended = self._record_cut(cut_id, written.path)
        return IngestResult(
            cut_path=written.path,
            manifest=written.manifest,
            cut_created=written.created,
            event=event,
            event_appended=appended,
        )

    def ingest_raw(
        self, raw_dir: Path, *, cut_id: str, seed: int, domains: Sequence[str] = SDTM_DOMAINS
    ) -> IngestResult:
        frames, sources = self.load_raw(raw_dir, domains)
        return self.ingest_frames(frames, cut_id=cut_id, seed=seed, sources=sources)

    def find_data_cut(self, cut_id: str) -> Event | None:
        for entry in self._ledger.read(event_types=[EventType.DATA_CUT]):
            if (
                isinstance(entry, Event)
                and isinstance(entry.payload, DataCut)
                and entry.payload.cut_id == cut_id
                and entry.payload.study_id == self._study_id
            ):
                return entry
        return None

    def _record_cut(self, cut_id: str, path: Path) -> tuple[Event, bool]:
        existing = self.find_data_cut(cut_id)
        if existing is not None:
            return existing, False
        event = self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=INGEST_ACTOR,
                study_id=self._study_id,
                visibility=Visibility.BLINDED,
                payload=DataCut(cut_id=cut_id, study_id=self._study_id, path=path.as_posix()),
            )
        )
        return event, True

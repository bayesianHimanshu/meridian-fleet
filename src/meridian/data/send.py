"""Nonclinical data (SEND): MRD-101's GLP rat study, from a public example study.

The source is PointCross PC201708 in the PHUSE phuse-scripts repository, pinned to one
commit (docs/preclinical-data.md). Each raw file's sha256 is pinned in config and checked
after the append-only fetch (meridian.data.fetch), and again before every ingest.

Curation is a pure transform: the source study id and the source treatment name are
replaced by Meridian's (MRD-101-TOX-01, MRD-101) in every text column, the source
USUBJID is kept as SRC_USUBJID, and every table is sorted by its keys. Nothing else
changes: values, units and SEND codes are as published.

Layout: <send_root>/<study>/<cut>/send/<domain>.parquet and manifest.json (a CutManifest,
so the readers and the inputs manifest work as for clinical cuts). The send/ area holds
dose groups by design (a toxicology study is analysed by group), so it is never written
through the clinical cut writer, whose blinded areas refuse arm and dose columns; only the
preclinical zone may read it (meridian.sandbox.policies).
"""

import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from meridian.data.cut_writer import (
    MANIFEST_NAME,
    CutExistsError,
    CutManifest,
    ManifestFile,
    SourceFile,
    render_manifest,
    write_parquet_deterministic,
)
from meridian.data.datacut import Ledger
from meridian.data.datacut import send_study_events as send_study_events
from meridian.data.fetch import FetchManifest, file_name
from meridian.data.files import sha256_file
from meridian.data.stage_layout import SEND
from meridian.data.xpt_reader import read_xpt
from meridian.domain.enums import Visibility
from meridian.domain.events import Event, EventDraft, SendStudyLoaded
from meridian.domain.ids import UtcDatetime

SEND_AREA = SEND
# The domains ingested: the design, mortality, and every findings domain the agent reads.
SEND_DOMAINS: tuple[str, ...] = (
    "bg", "bw", "cl", "dd", "dm", "ds", "ex", "fw", "lb", "ma", "mi", "om", "ts", "tx",
)  # fmt: skip
# Sort keys per domain; any other column order is kept as published.
_KEYS: dict[str, tuple[str, ...]] = {
    "ts": ("TSSEQ", "TSPARMCD"),
    "tx": ("SETCD", "TXSEQ"),
    "dm": ("USUBJID",),
}


class SendIngestError(Exception):
    pass


def check_pinned(raw_dir: Path, pinned: Mapping[str, str]) -> list[SourceFile]:
    """Every pinned domain file must be on disk, in the fetch manifest, and hash to its
    pinned sha256 (and so to the manifest's). Returns the sources in name order."""
    manifest = FetchManifest.load(raw_dir)
    sources: list[SourceFile] = []
    for domain in sorted(pinned):
        name = file_name(domain)
        entry = manifest.files.get(name)
        if entry is None:
            raise SendIngestError(f"{name} is not in {raw_dir}'s fetch manifest: fetch first")
        actual = sha256_file(raw_dir / name)
        if actual != pinned[domain] or entry.sha256 != pinned[domain]:
            raise SendIngestError(
                f"{name}: sha256 {actual} on disk, {entry.sha256} recorded, "
                f"{pinned[domain]} pinned in config; refusing to ingest"
            )
        sources.append(SourceFile(name=name, url=entry.url, sha256=actual))
    return sources


def load_raw(
    raw_dir: Path, pinned: Mapping[str, str]
) -> tuple[dict[str, pl.DataFrame], list[SourceFile]]:
    sources = check_pinned(raw_dir, pinned)
    frames = {d: read_xpt(raw_dir / file_name(d)) for d in sorted(pinned)}
    return frames, sources


def _sort_keys(domain: str, frame: pl.DataFrame) -> list[str]:
    keys = _KEYS.get(domain)
    if keys is None:
        seq = f"{domain.upper()}SEQ"
        keys = ("USUBJID", seq) if seq in frame.columns else ("USUBJID",)
    present = [k for k in keys if k in frame.columns]
    # Then every column, so rows equal on the keys still have one order.
    return present + [c for c in frame.columns if c not in present]


def curate(
    frames: Mapping[str, pl.DataFrame],
    *,
    study_id: str,
    source_study_id: str,
    treatment: str,
    source_treatment: str,
) -> dict[str, pl.DataFrame]:
    """Relabel and sort every domain. Pure: the same frames give the same tables."""
    unknown = sorted(set(frames) - set(SEND_DOMAINS))
    if unknown:
        raise SendIngestError(f"not SEND domains this ingest knows: {unknown}")
    out: dict[str, pl.DataFrame] = {}
    for domain in sorted(frames):
        frame = frames[domain]
        if "USUBJID" in frame.columns:
            frame = frame.with_columns(pl.col("USUBJID").alias("SRC_USUBJID"))
        text = [c for c, t in frame.schema.items() if t == pl.String and c != "SRC_USUBJID"]
        frame = frame.with_columns(
            pl.col(c)
            .str.replace_all(source_study_id, study_id, literal=True)
            .str.replace_all(source_treatment, treatment, literal=True)
            for c in text
        )
        if "STUDYID" in frame.columns:
            frame = frame.with_columns(pl.lit(study_id, dtype=pl.String).alias("STUDYID"))
        out[domain] = frame.sort(_sort_keys(domain, frame), maintain_order=True, nulls_last=True)
    return out


@dataclass(frozen=True, slots=True)
class SendCutResult:
    path: Path
    manifest: CutManifest
    created: bool


def write_send_cut(
    *,
    send_root: Path,
    study_id: str,
    cut_id: str,
    tables: Mapping[str, pl.DataFrame],
    created_at: UtcDatetime,
    sources: Sequence[SourceFile],
    seed: int = 0,
    seed_record: dict[str, object] | None = None,
    derived_from_cut: str | None = None,
    derived_from_manifest_sha256: str | None = None,
) -> SendCutResult:
    """Write <send_root>/<study>/<cut>/send/*.parquet and manifest.json atomically. A cut
    that exists with the same file hashes is left alone; different content is refused."""
    study_dir = send_root / study_id
    target = study_dir / cut_id
    study_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{cut_id}.tmp-", dir=study_dir))
    try:
        (tmp / SEND_AREA).mkdir()
        files: list[ManifestFile] = []
        for name in sorted(tables):
            relative = f"{SEND_AREA}/{name}.parquet"
            write_parquet_deterministic(tables[name], tmp / relative)
            files.append(
                ManifestFile(
                    path=relative,
                    sha256=sha256_file(tmp / relative),
                    rows=tables[name].height,
                    columns=tuple(tables[name].columns),
                )
            )
        manifest = CutManifest(
            cut_id=cut_id,
            study_id=study_id,
            created_at=created_at,
            seed=seed,
            seed_record=dict(seed_record or {}),
            files=tuple(files),
            sources=tuple(sorted(sources, key=lambda s: s.name)),
            derived_from_cut=derived_from_cut,
            derived_from_manifest_sha256=derived_from_manifest_sha256,
        )
        (tmp / MANIFEST_NAME).write_text(render_manifest(manifest), encoding="utf-8")
        if target.exists():
            existing = CutManifest.load(target)
            if existing.file_hashes() == manifest.file_hashes():
                return SendCutResult(path=target, manifest=existing, created=False)
            raise CutExistsError(
                f"SEND cut {cut_id} already exists at {target} with different content; "
                "cuts are never rewritten, use a new cut id"
            )
        tmp.rename(target)
        return SendCutResult(path=target, manifest=manifest, created=True)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def record_send_study(
    ledger: Ledger, *, occurred_at: UtcDatetime, actor: str, study_id: str, cut_id: str, path: Path
) -> tuple[Event, bool]:
    """Append SendStudyLoaded once per (study, cut). Returns (event, appended)."""
    for event in send_study_events(ledger, study_id):
        assert isinstance(event.payload, SendStudyLoaded)
        if event.payload.cut_id == cut_id:
            return event, False
    event = ledger.append(
        EventDraft(
            occurred_at=occurred_at,
            actor=actor,
            study_id=study_id,
            visibility=Visibility.BLINDED,
            payload=SendStudyLoaded(study_id=study_id, cut_id=cut_id, path=path.as_posix()),
        )
    )
    return event, True


@dataclass(frozen=True, slots=True)
class SendIngestResult:
    cut: SendCutResult
    event: Event
    event_appended: bool


def ingest_send(
    *,
    raw_dir: Path,
    pinned: Mapping[str, str],
    send_root: Path,
    study_id: str,
    cut_id: str,
    source_study_id: str,
    treatment: str,
    source_treatment: str,
    ledger: Ledger,
    occurred_at: UtcDatetime,
) -> SendIngestResult:
    """Check the raw files, curate, write the cut, record SendStudyLoaded (each once)."""
    frames, sources = load_raw(raw_dir, pinned)
    tables = curate(
        frames,
        study_id=study_id,
        source_study_id=source_study_id,
        treatment=treatment,
        source_treatment=source_treatment,
    )
    cut = write_send_cut(
        send_root=send_root,
        study_id=study_id,
        cut_id=cut_id,
        tables=tables,
        created_at=occurred_at,
        sources=sources,
    )
    event, appended = record_send_study(
        ledger,
        occurred_at=occurred_at,
        actor="system.ingest",
        study_id=study_id,
        cut_id=cut_id,
        path=cut.path,
    )
    return SendIngestResult(cut=cut, event=event, event_appended=appended)

"""A synthetic eTMF index, one snapshot per cut: metadata only, no documents.

The CDISC pilot has no Trial Master File, so one is generated from the cut's blinded DM
(site milestones) and config/tmf_expected.toml, seeded from a canonical form of the cut id.
Clean by construction: every expected artifact is present, QC passed, dated before the
milestone it serves and filed within its file_within_days.

The monitoring visit schedule is generated too (SDTM has no monitoring visits): per active
site an initiation visit 14 to 45 days before its first subject, routine visits every 6
to 10 weeks from the first subject while a subject is on study, and a close-out visit 14
to 42 days after its last subject. Each visit expects one report, dated the visit day. The
export date is 60 days after the last close-out visit.

Harmless variation a TMF checker must not flag, recorded by document id in the manifest's
seed_record.harmless:

* versions: the Investigator's Brochure gets an update every year and the Monitoring
  Plan a second version; only the latest counts.
* superseded_late: a site's PI CV has a superseded version 1 that was filed late, and a
  current version 2 filed on time.
* name_variant: the artifact name indexed with other case and extra whitespace.
* same_day: filed the same day as its document date.
* qc_resubmission: a delegation log first rejected at QC, then a passed version 2 with
  the same document date.
* report_offset: a visit report dated 1 to 3 days after its visit (within
  visit_match_days).

Layout: <etmf_root>/<study>/<cut>/{blinded/etmf_index.parquet,
blinded/monitoring_visits.parquet, blinded/etmf_export.parquet, manifest.json}, written by
the cut writer (atomic, deterministic, never rewritten). Staged into /stage/tmf for the
quality zone only.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from meridian.data.cut_writer import CutWriteResult, write_cut
from meridian.domain import tmf as t
from meridian.faults.reader import FullCut
from meridian.platform.clock import Clock
from meridian.platform.tmf_reference import ExpectedArtifact, TmfReference
from meridian.tmf.milestones import SiteMilestones, site_milestones, trial_first_subject

GENERATOR = "meridian.tmf.generate"
GENERATOR_VERSION = 1
EXPORT_AFTER_DAYS = 60
INITIATION_BEFORE = (14, 46)  # days before the first subject, low inclusive, high exclusive
ROUTINE_EVERY = (42, 71)  # 6 to 10 weeks
CLOSE_OUT_AFTER = (14, 43)
AMENDMENTS = (1, 3)
# Harmless variation, by name, and the share of candidates that get it.
VERSIONS = "versions"
SUPERSEDED_LATE = "superseded_late"
NAME_VARIANT = "name_variant"
SAME_DAY = "same_day"
QC_RESUBMISSION = "qc_resubmission"
REPORT_OFFSET = "report_offset"
HARMLESS: tuple[str, ...] = (
    VERSIONS,
    SUPERSEDED_LATE,
    NAME_VARIANT,
    SAME_DAY,
    QC_RESUBMISSION,
    REPORT_OFFSET,
)
SHARE: dict[str, float] = {
    SUPERSEDED_LATE: 0.25,
    NAME_VARIANT: 0.12,
    SAME_DAY: 0.1,
    QC_RESUBMISSION: 0.2,
    REPORT_OFFSET: 0.15,
}
# How long before a milestone each activation record is dated (days, low, high exclusive).
DATED_BEFORE_ACTIVATION: dict[str, tuple[int, int]] = {
    "irb_iec_decision": (10, 41),
    "form_fda_1572": (5, 31),
    "pi_cv": (30, 301),
    "site_signature_sheet": (0, 8),
    "protocol_signature_page": (5, 31),
    "financial_disclosure_form": (5, 41),
}
DATED_BEFORE_TRIAL: dict[str, tuple[int, int]] = {
    "protocol": (150, 241),
    "investigators_brochure": (120, 201),
    "sample_crf": (60, 121),
    "informed_consent_form": (90, 151),
    "monitoring_plan": (30, 91),
    "regulatory_decision": (30, 61),
}


def snapshot_seed(cut_id: str, salt: str = "etmf") -> int:
    """A seed from the canonical form "<salt>|<cut id>": the same cut, the same index."""
    digest = hashlib.sha256(f"{salt}|{cut_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True, slots=True)
class Visit:
    visit_id: str
    site: str
    visit_type: str
    day: date


@dataclass(slots=True)
class Index:
    """What the eTMF exports for one cut."""

    documents: list[dict[str, str]] = field(default_factory=list)
    visits: list[Visit] = field(default_factory=list)
    export_date: date = date(1970, 1, 1)
    harmless: dict[str, list[str]] = field(default_factory=lambda: {h: [] for h in HARMLESS})

    def frames(self) -> dict[str, pl.DataFrame]:
        index = pl.DataFrame(
            [{c: d[c] for c in t.INDEX_COLUMNS} for d in self.documents],
            schema=dict.fromkeys(t.INDEX_COLUMNS, pl.String),
        ).sort(t.DOCUMENT_ID)
        visits = pl.DataFrame(
            [
                {
                    t.VISIT_ID: v.visit_id,
                    t.SITE: v.site,
                    t.VISIT_TYPE: v.visit_type,
                    t.VISIT_DATE: v.day.isoformat(),
                }
                for v in self.visits
            ],
            schema=dict.fromkeys(t.VISIT_COLUMNS, pl.String),
        ).sort(t.VISIT_ID)
        export = pl.DataFrame(
            {t.EXPORT_DATE: [self.export_date.isoformat()]},
            schema=dict.fromkeys(t.EXPORT_COLUMNS, pl.String),
        )
        return {t.ETMF_INDEX: index, t.MONITORING_VISITS: visits, t.ETMF_EXPORT: export}


def _opaque(prefix: str, seed: int, n: int) -> str:
    return f"{prefix}-" + hashlib.sha256(f"{seed}|{prefix}|{n}".encode()).hexdigest()[:10].upper()


def _days(rng: np.random.Generator, span: tuple[int, int]) -> timedelta:
    return timedelta(days=int(rng.integers(*span)))


def _variant(name: str, rng: np.random.Generator) -> str:
    words = name.split()
    style = int(rng.integers(0, 3))
    if style == 0:
        return "  ".join(w.lower() for w in words)
    if style == 1:
        return f" {name.upper()} "
    return " ".join(w.swapcase() if i % 2 else w for i, w in enumerate(words)) + " "


class _Builder:
    def __init__(self, ref: TmfReference, seed: int, rng: np.random.Generator) -> None:
        self.ref = ref
        self.seed = seed
        self.rng = rng
        self.index = Index()
        self._n = 0

    def visit(self, site: str, visit_type: str, day: date) -> Visit:
        self._n += 1
        v = Visit(_opaque("MV", self.seed, self._n), site, visit_type, day)
        self.index.visits.append(v)
        return v

    def document(
        self,
        artifact: ExpectedArtifact,
        site: str,
        version: int,
        day: date,
        *,
        filed: date | None = None,
        qc: str = t.QC_PASSED,
        variation: bool = True,
    ) -> str:
        """One filed version, on time unless filed is given; harmless name and same-day
        variation drawn here."""
        self._n += 1
        doc_id = _opaque("TMF", self.seed, self._n)
        name = artifact.name
        if variation and self.rng.random() < SHARE[NAME_VARIANT]:
            name = _variant(name, self.rng)
            self.index.harmless[NAME_VARIANT].append(doc_id)
        if filed is None:
            if variation and self.rng.random() < SHARE[SAME_DAY]:
                filed = day
                self.index.harmless[SAME_DAY].append(doc_id)
            else:
                filed = day + timedelta(days=int(self.rng.integers(1, artifact.file_within_days)))
        self.index.documents.append(
            {
                t.DOCUMENT_ID: doc_id,
                t.ARTIFACT: name,
                t.LEVEL: artifact.level,
                t.SITE: site,
                t.DOC_SITE: site,
                t.VERSION: str(version),
                t.DOCUMENT_DATE: day.isoformat(),
                t.FILED_DATE: filed.isoformat(),
                t.QC_STATUS: qc,
            }
        )
        return doc_id


def generate(dm: pl.DataFrame, ref: TmfReference, seed: int) -> Index:
    """The clean eTMF index of one cut."""
    rng = np.random.Generator(np.random.PCG64(seed))
    b = _Builder(ref, seed, rng)
    art = ref.by_key()
    sites = [m for m in site_milestones(dm).values() if m.first_subject and m.last_subject]
    if not sites:
        return b.index
    first = trial_first_subject({m.site: m for m in sites})
    assert first is not None
    schedule = {m.site: _schedule(b, m) for m in sites}
    last_close = max(v.day for vs in schedule.values() for v in vs)
    export = last_close + timedelta(days=EXPORT_AFTER_DAYS)
    b.index.export_date = export
    # Trial level.
    trial_days: dict[str, date] = {}
    for key, span in DATED_BEFORE_TRIAL.items():
        trial_days[key] = first - _days(rng, span)
        b.document(art[key], "", 1, trial_days[key])
    ib = art["investigators_brochure"]
    version = 2
    update = trial_days["investigators_brochure"] + timedelta(days=365)
    while update + timedelta(days=ib.file_within_days) < export:
        b.index.harmless[VERSIONS].append(b.document(ib, "", version, update))
        version += 1
        update += timedelta(days=365)
    mp = art["monitoring_plan"]
    mp_day = first + _days(rng, (120, 240))
    if mp_day + timedelta(days=mp.file_within_days) < export:  # a short trial may not get one
        b.index.harmless[VERSIONS].append(b.document(mp, "", 2, mp_day))
    amendments = sorted(
        day
        for day in (first + _days(rng, (90, 400)) for _ in range(int(rng.integers(*AMENDMENTS))))
        if day + timedelta(days=60) < export
    )
    for i, day in enumerate(amendments, start=1):
        b.document(art["protocol_amendment"], "", i, day)
    # Site level.
    for m in sites:
        visits = schedule[m.site]
        initiation = visits[0].day
        assert m.first_subject is not None
        for key, span in DATED_BEFORE_ACTIVATION.items():
            a = art[key]
            day = initiation - _days(rng, span)
            if key == "pi_cv" and rng.random() < SHARE[SUPERSEDED_LATE]:
                old = day - _days(rng, (200, 700))
                late = old + timedelta(days=a.file_within_days + int(rng.integers(5, 90)))
                b.index.harmless[SUPERSEDED_LATE].append(
                    b.document(a, m.site, 1, old, filed=min(late, day), variation=False)
                )
                b.document(a, m.site, 2, day)
            elif key == "site_signature_sheet" and rng.random() < SHARE[QC_RESUBMISSION]:
                b.index.harmless[QC_RESUBMISSION].append(
                    b.document(a, m.site, 1, day, filed=day, qc=t.QC_REJECTED, variation=False)
                )
                again = day + timedelta(days=int(rng.integers(1, a.file_within_days)))
                b.document(a, m.site, 2, day, filed=again, variation=False)
            else:
                b.document(a, m.site, 1, day)
        # Each amendment's approval at the site (later versions of the IRB decision).
        irb = art["irb_iec_decision"]
        for i, day in enumerate(amendments, start=2):
            approved = day + _days(rng, (14, 46))
            if approved + timedelta(days=irb.file_within_days) < export and approved > initiation:
                b.document(irb, m.site, i, approved)
        for v in visits:
            report = ref.for_visit(v.visit_type)
            assert report is not None
            if rng.random() < SHARE[REPORT_OFFSET]:
                late = v.day + timedelta(days=int(rng.integers(1, ref.rules.visit_match_days + 1)))
                b.index.harmless[REPORT_OFFSET].append(b.document(report, m.site, 1, late))
            else:
                b.document(report, m.site, 1, v.day)
    _pending_when_recent(b.index, ref)
    return b.index


def _schedule(b: _Builder, m: SiteMilestones) -> list[Visit]:
    assert m.first_subject is not None
    assert m.last_subject is not None
    rng = b.rng
    out = [b.visit(m.site, t.INITIATION, m.first_subject - _days(rng, INITIATION_BEFORE))]
    day = m.first_subject + _days(rng, ROUTINE_EVERY)
    while day <= m.last_subject:
        out.append(b.visit(m.site, t.ROUTINE, day))
        day += _days(rng, ROUTINE_EVERY)
    out.append(b.visit(m.site, t.CLOSE_OUT, m.last_subject + _days(rng, CLOSE_OUT_AFTER)))
    return out


def _pending_when_recent(index: Index, ref: TmfReference) -> None:
    """A document filed within qc_within_days of the export is still pending QC, as it
    would be; nothing older is."""
    limit = index.export_date - timedelta(days=ref.rules.qc_within_days)
    for d in index.documents:
        if d[t.QC_STATUS] == t.QC_PASSED and date.fromisoformat(d[t.FILED_DATE]) > limit:
            d[t.QC_STATUS] = t.QC_PENDING


def write_snapshot(
    *,
    etmf_root: Path,
    study_id: str,
    cut: FullCut,
    index: Index,
    seed: int,
    clock: Clock,
    extra_record: dict[str, Any] | None = None,
) -> CutWriteResult:
    return write_cut(
        curated_root=etmf_root,
        study_id=study_id,
        cut_id=cut.manifest.cut_id,
        blinded=index.frames(),
        ops={},
        unblinded={},
        created_at=clock.now(),
        seed=seed,
        seed_record={
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "harmless": {h: sorted(ids) for h, ids in sorted(index.harmless.items())},
            **(extra_record or {}),
        },
        sources=[],
        derived_from_cut=cut.manifest.cut_id,
        derived_from_manifest_sha256=cut.manifest_sha256,
    )


def ensure_clean_snapshot(
    *, etmf_root: Path, study_id: str, cut: FullCut, ref: TmfReference, clock: Clock
) -> CutWriteResult:
    """The clean eTMF snapshot of a cut (written once; identical content is a no-op)."""
    seed = snapshot_seed(cut.manifest.cut_id)
    index = generate(cut.blinded["dm"], ref, seed)
    return write_snapshot(
        etmf_root=etmf_root, study_id=study_id, cut=cut, index=index, seed=seed, clock=clock
    )

"""A synthetic safety database, one snapshot per cut, from the cut's clinical SAEs.

The CDISC pilot has no safety database, so one is generated: one case per serious AE
(AESER = Y), seeded from a canonical form of the cut id, so the same cut always gets the
same cases. Clean by construction: every clinical SAE has exactly one case, the fields
agree, awareness is onset plus 0 to 3 days, and receipt is within 24 hours of awareness.

Some cases carry harmless differences a reconciler must not flag: a preferred term in
another case or with extra whitespace, a reporter's verbatim term that differs from the
clinical term (the preferred terms match), and an onset given as a date-time on the same
day. A few related cases carry the subject's treatment, as an expedited (SUSAR) report
would; that column is split off at ingestion into unblinded/, like DM's treatment
columns, so the blinded case table never holds it.

Layout: <safety_root>/<study>/<cut>/{blinded/safety_cases.parquet,
unblinded/safety_unblinded.parquet, manifest.json}, written by the cut writer (atomic,
deterministic, blinding checked before a byte is written, never rewritten).
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from meridian.data.blinding import forbidden_columns_in
from meridian.data.cut_writer import CutWriteResult, write_cut
from meridian.domain import safety as s
from meridian.faults.reader import FullCut
from meridian.platform.clock import Clock

GENERATOR = "meridian.safetydb.generate"
GENERATOR_VERSION = 1
HARMLESS_SHARE = 0.35  # each harmless difference, independently, per case
TREATMENT_SHARE = 0.5  # of related cases, how many carry the treatment (SUSAR-like)
RELATED = frozenset({"POSSIBLE", "PROBABLE", "DEFINITE", "RELATED"})
AWARE_MAX_DAYS = 3
RECEIPT_MIN_MINUTES = 5
RECEIPT_MAX_MINUTES = 24 * 60 - 5  # strictly within 24 hours of awareness

# The harmless difference types, in the order they are drawn.
TERM_CASE = "term_case_whitespace"
VERBATIM = "verbatim_term"
DATETIME = "datetime_onset"
HARMLESS: tuple[str, ...] = (TERM_CASE, VERBATIM, DATETIME)


def snapshot_seed(cut_id: str, salt: str = "safety_db") -> int:
    """A seed from the canonical form "<salt>|<cut id>": the same cut, the same cases."""
    digest = hashlib.sha256(f"{salt}|{cut_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def case_id(seed: int, usubjid: str, aeseq: int) -> str:
    """Opaque: says nothing about the subject or the site."""
    return "SC-" + hashlib.sha256(f"{seed}|{usubjid}|{aeseq}".encode()).hexdigest()[:10].upper()


@dataclass(frozen=True, slots=True)
class Export:
    """What the safety database exports for one cut, before ingestion splits it."""

    cases: pl.DataFrame  # CASE_COLUMNS plus TRTA (null for most cases)
    harmless: dict[str, list[str]]  # harmless difference type -> case ids


def _variant_term(term: str, rng: np.random.Generator) -> str:
    """The same preferred term in another case and with extra whitespace."""
    words = term.split()
    style = int(rng.integers(0, 3))
    cased = [w.lower() for w in words] if style == 0 else [w.capitalize() for w in words]
    if style == 2:
        cased = [w.swapcase() if i % 2 else w.lower() for i, w in enumerate(words)]
    text = "  ".join(cased)
    return f" {text} " if text.upper() == term else f" {text}"


def _verbatim(term: str) -> str:
    return f"episode of {term.lower()} reported by site"


def clinical_saes(ae: pl.DataFrame) -> pl.DataFrame:
    return ae.filter(pl.col("AESER").str.strip_chars().str.to_uppercase() == "Y").sort(
        ["USUBJID", "AESEQ"]
    )


def generate(cut: FullCut, seed: int) -> Export:
    """The clean export of one cut's clinical SAEs."""
    rng = np.random.Generator(np.random.PCG64(seed))
    ae = clinical_saes(cut.blinded["ae"])
    sites = dict(cut.blinded["dm"].select("USUBJID", "SITEID").sort("USUBJID").iter_rows())
    arms = dict(
        cut.unblinded["randomization"].select("USUBJID", "ACTARM").sort("USUBJID").iter_rows()
    )
    rows: list[dict[str, Any]] = []
    harmless: dict[str, list[str]] = {h: [] for h in HARMLESS}
    for r in ae.iter_rows(named=True):
        cid = case_id(seed, r["USUBJID"], int(r["AESEQ"]))
        draws = {h: bool(rng.random() < HARMLESS_SHARE) for h in HARMLESS}
        term = str(r["AEDECOD"])
        onset_text = str(r["AESTDTC"] or "")
        day = s.onset_day(onset_text)
        onset_time = time(int(rng.integers(6, 22)), int(rng.integers(0, 60)))
        if draws[DATETIME] and day is not None and len(onset_text) == 10:
            onset_text = f"{onset_text}T{onset_time.strftime('%H:%M')}"
        else:
            draws[DATETIME] = False
        start = (
            datetime.combine(day, onset_time if draws[DATETIME] else time(0), tzinfo=UTC)
            if day is not None
            else None
        )
        aware_minutes = int(rng.integers(0, AWARE_MAX_DAYS * 24 * 60 + 1))
        receipt_minutes = int(rng.integers(RECEIPT_MIN_MINUTES, RECEIPT_MAX_MINUTES + 1))
        aware = start + timedelta(minutes=aware_minutes) if start is not None else None
        receipt = aware + timedelta(minutes=receipt_minutes) if aware is not None else None
        related = str(r["AEREL"] or "").strip().upper() in RELATED
        carries_treatment = related and bool(rng.random() < TREATMENT_SHARE)
        row: dict[str, Any] = {
            s.CASEID: cid,
            s.USUBJID: r["USUBJID"],
            s.SITEID: sites.get(r["USUBJID"], ""),
            s.REPORTED_TERM: _verbatim(term) if draws[VERBATIM] else str(r["AETERM"]),
            s.PREFERRED_TERM: _variant_term(term, rng) if draws[TERM_CASE] else term,
            s.ONSET: onset_text,
            **{case_col: str(r.get(ae_col) or "N") for case_col, ae_col in s.CRITERIA.items()},
            s.OUTCOME: str(r["AEOUT"] or ""),
            s.CAUSALITY: str(r["AEREL"] or ""),
            s.AWARE_DTC: aware.isoformat() if aware is not None else "",
            s.RECEIPT_DTC: receipt.isoformat() if receipt is not None else "",
            s.TREATMENT: arms.get(r["USUBJID"]) if carries_treatment else None,
        }
        rows.append(row)
        for h in HARMLESS:
            if draws[h]:
                harmless[h].append(cid)
    schema = {c: pl.String for c in (*s.CASE_COLUMNS, s.TREATMENT)}
    return Export(cases=pl.DataFrame(rows, schema=schema).sort(s.CASEID), harmless=harmless)


def split(cases: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Ingestion: the blinded case table, and the treatment by case id for unblinded/.
    Every column the blinding rules forbid leaves the blinded table."""
    forbidden = forbidden_columns_in(cases.columns)
    blinded = cases.drop(forbidden).sort(s.CASEID)
    unblinded = (
        cases.filter(pl.col(s.TREATMENT).is_not_null()).select(s.CASEID, *forbidden).sort(s.CASEID)
    )
    return blinded, unblinded


def write_snapshot(
    *,
    safety_root: Path,
    study_id: str,
    cut: FullCut,
    cases: pl.DataFrame,
    harmless: dict[str, list[str]],
    seed: int,
    clock: Clock,
    extra_record: dict[str, Any] | None = None,
) -> CutWriteResult:
    blinded, unblinded = split(cases)
    return write_cut(
        curated_root=safety_root,
        study_id=study_id,
        cut_id=cut.manifest.cut_id,
        blinded={s.SAFETY_CASES: blinded},
        ops={},
        unblinded={s.SAFETY_UNBLINDED: unblinded},
        created_at=clock.now(),
        seed=seed,
        seed_record={
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "harmless": {h: sorted(ids) for h, ids in sorted(harmless.items())},
            **(extra_record or {}),
        },
        sources=[],
        derived_from_cut=cut.manifest.cut_id,
        derived_from_manifest_sha256=cut.manifest_sha256,
    )


def ensure_clean_snapshot(
    *, safety_root: Path, study_id: str, cut: FullCut, clock: Clock
) -> CutWriteResult:
    """The clean snapshot of a cut (written once; identical content is a no-op)."""
    seed = snapshot_seed(cut.manifest.cut_id)
    export = generate(cut, seed)
    return write_snapshot(
        safety_root=safety_root,
        study_id=study_id,
        cut=cut,
        cases=export.cases,
        harmless=export.harmless,
        seed=seed,
        clock=clock,
    )

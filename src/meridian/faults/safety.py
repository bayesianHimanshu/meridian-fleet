"""Safety faults: record-level discrepancies between the clinical database and the safety
database, planted into seeded evaluation cuts, with ground truth per record.

A safety scenario is a canonical form (set, index, seed, base cut, augmentation, faults);
its cut id is "DC-" plus the first 8 hex characters of sha256 of that canonical JSON, not
of any file's bytes, so the same scenario always names the same cut.

The base cut has only 3 serious AEs, so every evaluation cut first promotes a seeded
handful of non-serious AEs to serious (AESER = Y, AESHOSP = Y), the augmentation, before
its safety database is generated. Faults are then planted on distinct records:

* sae_missing_in_safety_db: the case of a clinical SAE is removed.
* case_without_clinical_sae: the clinical AE of a case is removed (variant 0) or recorded
  non-serious (variant 1, AESER = N).
* onset_mismatch: the case onset moves by 2 or 3 days (awareness and receipt move with
  it). More than the fallback window would read as a missing case plus an orphan case.
* outcome_mismatch, causality_mismatch: the case carries another value.
* late_sae_report: receipt more than 24 hours after awareness, at a site with no other
  late case.
* late_reporting_site: most of one site's cases (at least 2, and at least three quarters)
  arrive late; the ground truth holds each late case and the site.
"""

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict

from meridian.data.cut_writer import write_cut
from meridian.data.datacut import Ledger, record_data_cut
from meridian.data.files import write_bytes_atomic
from meridian.domain import safety as s
from meridian.domain.enums import FindingKind
from meridian.domain.events import Event
from meridian.faults.catalog import FaultError, FaultRecord
from meridian.faults.inject import FAULTS_ACTOR, GroundTruth
from meridian.faults.reader import FullCut, FullCutReader
from meridian.ledger.hashing import canonical_json
from meridian.platform.clock import Clock
from meridian.safetydb.generate import case_id, clinical_saes, generate, write_snapshot

LATE_SITE_FAULT = "late_reporting_site"
FAULT_TYPES: tuple[str, ...] = (
    s.SAE_MISSING,
    s.CASE_WITHOUT_SAE,
    s.ONSET_MISMATCH,
    s.OUTCOME_MISMATCH,
    s.CAUSALITY_MISMATCH,
    s.LATE_REPORT,
    LATE_SITE_FAULT,
)
OUTCOMES: tuple[str, ...] = (
    "RECOVERED/RESOLVED",
    "RECOVERING/RESOLVING",
    "NOT RECOVERED/NOT RESOLVED",
    "RECOVERED/RESOLVED WITH SEQUELAE",
    "FATAL",
)
CAUSALITIES: tuple[str, ...] = ("NONE", "REMOTE", "POSSIBLE", "PROBABLE")
SITE_LATE_SHARE = 0.75
SITE_LATE_MIN = 2
SetName = Literal["dev", "holdout", "clean"]
SET_SEEDS: dict[str, int] = {"dev": 20261110, "holdout": 20261111, "clean": 20261112}
SET_SIZES: dict[str, int] = {"dev": 30, "holdout": 30, "clean": 10}
AUGMENT_RANGE = (6, 13)  # promoted AEs per cut, low inclusive, high exclusive
MAX_FAULTS = 3
BASE_CUT = "DC-BASE"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SafetyFault(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    params: dict[str, float]


class SafetyScenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    set_name: SetName
    index: int
    seed: int
    base_cut: str
    augment: int
    faults: tuple[SafetyFault, ...]

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    @property
    def cut_id(self) -> str:
        return "DC-" + sha256_hex(self.canonical().encode("utf-8"))[:8]

    @property
    def label(self) -> str:
        return f"safety_{self.set_name}_{self.index:02d}"


@dataclass(frozen=True, slots=True)
class Planted:
    ae: pl.DataFrame
    cases: pl.DataFrame  # the export after faults, TRTA included
    harmless: dict[str, list[str]]
    records: tuple[FaultRecord, ...]
    case_seed: int


def _children(seed: int, n: int) -> list[np.random.Generator]:
    return [np.random.Generator(np.random.PCG64(c)) for c in np.random.SeedSequence(seed).spawn(n)]


def augment(ae: pl.DataFrame, dm: pl.DataFrame, n: int, rng: np.random.Generator) -> pl.DataFrame:
    """Promote n seeded non-serious AEs with a full onset date, of treated subjects, to
    serious. A (subject, preferred term) that is already serious, or that occurs twice
    among the candidates, is left out, so no two SAEs could match the same case."""
    treated = set(
        dm.filter(pl.col("RFSTDTC").fill_null("").str.strip_chars() != "")["USUBJID"].to_list()
    )
    serious = clinical_saes(ae)
    taken = {(u, s.norm_text(t)) for u, t in serious.select("USUBJID", "AEDECOD").iter_rows()}
    pool = ae.filter(
        (pl.col("AESER").str.strip_chars().str.to_uppercase() == "N")
        & (pl.col("AESTDTC").fill_null("").str.len_chars() == 10)
        & pl.col("USUBJID").is_in(sorted(treated))
    ).sort(["USUBJID", "AESEQ"])
    keys = [(u, s.norm_text(t)) for u, t in pool.select("USUBJID", "AEDECOD").iter_rows()]
    counts: dict[tuple[str, str], int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    rows = [i for i, k in enumerate(keys) if counts[k] == 1 and k not in taken]
    if len(rows) < n:
        raise FaultError(f"only {len(rows)} AEs can be promoted to serious; {n} asked")
    chosen = sorted(int(i) for i in rng.choice(rows, size=n, replace=False))
    promote = pool[chosen].select("USUBJID", "AESEQ").with_columns(pl.lit(True).alias("_p"))
    out = ae.join(promote, on=["USUBJID", "AESEQ"], how="left", maintain_order="left")
    return out.with_columns(
        pl.when(pl.col("_p")).then(pl.lit("Y")).otherwise(pl.col("AESER")).alias("AESER"),
        pl.when(pl.col("_p")).then(pl.lit("Y")).otherwise(pl.col("AESHOSP")).alias("AESHOSP"),
    ).drop("_p")


def _shift(text: str, days: int) -> str:
    day = s.onset_day(text)
    assert day is not None
    return (day + timedelta(days=days)).isoformat() + text[10:]


def _record(
    fault_id: str,
    fault_type: str,
    target: str,
    check: str,
    params: dict[str, float],
    touched: dict[str, int],
    achieved: dict[str, float],
) -> FaultRecord:
    return FaultRecord(
        fault_id=fault_id,
        fault_type=fault_type,
        target=target,
        params=params,
        expected_kind=FindingKind.SAE_RECONCILIATION,
        kri=check,
        rows_touched=touched,
        achieved=achieved,
    )


def plant(scenario: SafetyScenario, base: FullCut) -> Planted:
    """Augment, generate the clean safety database, then plant each fault on its own
    record. Each step has its own child of SeedSequence(scenario.seed)."""
    rngs = _children(scenario.seed, 2 + len(scenario.faults))
    ae = augment(base.blinded["ae"], base.blinded["dm"], scenario.augment, rngs[0])
    case_seed = int(rngs[1].integers(0, 2**62))
    clinical = replace(base, blinded={**base.blinded, "ae": ae})
    export = generate(clinical, case_seed)
    cases = export.cases
    saes = clinical_saes(ae)
    sites = dict(base.blinded["dm"].select("USUBJID", "SITEID").sort("USUBJID").iter_rows())
    records_of: list[tuple[str, str, int]] = [
        (u, sites[u], int(q)) for u, q in saes.select("USUBJID", "AESEQ").iter_rows()
    ]
    used: set[tuple[str, int]] = set()
    late_sites: set[str] = set()
    planted: list[FaultRecord] = []

    def cid(u: str, q: int) -> str:
        return case_id(case_seed, u, q)

    def free(exclude_sites: set[str] | None = None) -> list[tuple[str, str, int]]:
        return [
            r
            for r in records_of
            if (r[0], r[2]) not in used and (exclude_sites is None or r[1] not in exclude_sites)
        ]

    def set_case(caseid: str, **values: str | None) -> None:
        nonlocal cases
        cases = cases.with_columns(
            pl.when(pl.col(s.CASEID) == caseid)
            .then(pl.lit(v, dtype=pl.String))
            .otherwise(pl.col(k))
            .alias(k)
            for k, v in values.items()
        )

    def case_row(caseid: str) -> dict[str, str | None]:
        rows = cases.filter(pl.col(s.CASEID) == caseid).to_dicts()
        assert len(rows) == 1, caseid
        return rows[0]

    def make_late(caseid: str, hours: float) -> None:
        aware = s.parse_dtc(str(case_row(caseid)[s.AWARE_DTC]))
        set_case(caseid, **{s.RECEIPT_DTC: (aware + timedelta(hours=hours)).isoformat()})

    # Whole-site faults first, so the single faults can keep away from their site.
    order = sorted(
        range(len(scenario.faults)), key=lambda i: scenario.faults[i].type != LATE_SITE_FAULT
    )
    for i in order:
        fault = scenario.faults[i]
        rng = rngs[2 + i]
        fid = f"F-{i + 1:03d}"
        p = fault.params
        if fault.type == LATE_SITE_FAULT:
            by_site: dict[str, list[tuple[str, str, int]]] = {}
            for r in records_of:
                by_site.setdefault(r[1], []).append(r)
            eligible = []
            for site in sorted(by_site):
                total = len(by_site[site])
                need = max(SITE_LATE_MIN, math.ceil(SITE_LATE_SHARE * total))
                open_ = [r for r in by_site[site] if (r[0], r[2]) not in used]
                if site not in late_sites and len(open_) >= need and total >= SITE_LATE_MIN:
                    eligible.append((site, open_[:need], total))
            if not eligible:
                raise FaultError("no site has enough serious AEs for late_reporting_site")
            site, late, total = eligible[int(rng.integers(0, len(eligible)))]
            for j, (u, _, q) in enumerate(late, start=1):
                hours = p["delay_hours"] + 12.0 * j
                make_late(cid(u, q), hours)
                used.add((u, q))
                planted.append(
                    _record(
                        f"{fid}.{j}",
                        LATE_SITE_FAULT,
                        cid(u, q),
                        s.LATE_REPORT,
                        p,
                        {s.SAFETY_CASES: 1},
                        {"delay_hours": hours},
                    )
                )
            late_sites.add(site)
            planted.append(
                _record(
                    fid,
                    LATE_SITE_FAULT,
                    site,
                    s.LATE_SITE,
                    p,
                    {s.SAFETY_CASES: len(late)},
                    {"late_cases": float(len(late)), "site_cases": float(total)},
                )
            )
            continue
        pool = free(late_sites if fault.type == s.LATE_REPORT else None)
        if not pool:
            raise FaultError(f"no record left for {fault.type}")
        u, site, q = pool[int(rng.integers(0, len(pool)))]
        used.add((u, q))
        key, caseid = s.clinical_key(u, q), cid(u, q)
        if fault.type == s.SAE_MISSING:
            cases = cases.filter(pl.col(s.CASEID) != caseid)
            planted.append(_record(fid, fault.type, key, s.SAE_MISSING, p, {s.SAFETY_CASES: 1}, {}))
        elif fault.type == s.CASE_WITHOUT_SAE:
            hit = (pl.col("USUBJID") == u) & (pl.col("AESEQ") == q)
            if p["variant"] == 0:
                ae = ae.filter(~hit)
            else:
                ae = ae.with_columns(
                    pl.when(hit).then(pl.lit("N")).otherwise(pl.col("AESER")).alias("AESER")
                )
            planted.append(
                _record(
                    fid,
                    fault.type,
                    caseid,
                    s.CASE_WITHOUT_SAE,
                    p,
                    {"ae": 1},
                    {"variant": p["variant"]},
                )
            )
        elif fault.type == s.ONSET_MISMATCH:
            row = case_row(caseid)
            days = int(p["shift_days"])
            moved = {
                s.ONSET: _shift(str(row[s.ONSET]), days),
                s.AWARE_DTC: (
                    s.parse_dtc(str(row[s.AWARE_DTC])) + timedelta(days=days)
                ).isoformat(),
                s.RECEIPT_DTC: (
                    s.parse_dtc(str(row[s.RECEIPT_DTC])) + timedelta(days=days)
                ).isoformat(),
            }
            set_case(caseid, **moved)
            planted.append(
                _record(
                    fid,
                    fault.type,
                    key,
                    s.ONSET_MISMATCH,
                    p,
                    {s.SAFETY_CASES: 1},
                    {"shift_days": float(days)},
                )
            )
        elif fault.type in (s.OUTCOME_MISMATCH, s.CAUSALITY_MISMATCH):
            column = s.OUTCOME if fault.type == s.OUTCOME_MISMATCH else s.CAUSALITY
            vocabulary = OUTCOMES if fault.type == s.OUTCOME_MISMATCH else CAUSALITIES
            current = s.norm_text(case_row(caseid)[column])
            others = [v for v in vocabulary if v != current]
            set_case(caseid, **{column: others[int(rng.integers(0, len(others)))]})
            planted.append(_record(fid, fault.type, key, fault.type, p, {s.SAFETY_CASES: 1}, {}))
        elif fault.type == s.LATE_REPORT:
            make_late(caseid, p["delay_hours"])
            late_sites.add(site)
            planted.append(
                _record(
                    fid,
                    fault.type,
                    caseid,
                    s.LATE_REPORT,
                    p,
                    {s.SAFETY_CASES: 1},
                    {"delay_hours": p["delay_hours"]},
                )
            )
        else:
            raise FaultError(f"unknown safety fault {fault.type!r}")
    records = tuple(sorted(planted, key=lambda r: r.fault_id))
    return Planted(ae, cases.sort(s.CASEID), export.harmless, records, case_seed)


def _fault(fault_type: str, rng: np.random.Generator) -> SafetyFault:
    if fault_type == s.CASE_WITHOUT_SAE:
        return SafetyFault(type=fault_type, params={"variant": float(rng.integers(0, 2))})
    if fault_type == s.ONSET_MISMATCH:
        size = int(rng.integers(2, 4))
        return SafetyFault(
            type=fault_type, params={"shift_days": float(size * rng.choice([-1, 1]))}
        )
    if fault_type in (s.LATE_REPORT, LATE_SITE_FAULT):
        return SafetyFault(type=fault_type, params={"delay_hours": float(rng.integers(30, 241))})
    return SafetyFault(type=fault_type, params={})


def scenario_set(set_name: SetName, base: FullCut, n: int | None = None) -> list[SafetyScenario]:
    """The fixed evaluation set: n seeded scenarios, each planting 1 to 3 faults of
        distinct types (none for the clean set). A draw that cannot be planted on the base
        is drawn again from the same generator, so the set stays deterministic. Fault types
    are spread evenly: each scenario takes the types used least so far."""
    rng = np.random.Generator(np.random.PCG64(SET_SEEDS[set_name]))
    out: list[SafetyScenario] = []
    size = SET_SIZES[set_name] if n is None else n
    used = dict.fromkeys(FAULT_TYPES, 0)
    while len(out) < size:
        k = 0 if set_name == "clean" else int(rng.integers(1, MAX_FAULTS + 1))
        # The k least used types so far, ties broken at random: every type gets used.
        ties = rng.random(len(FAULT_TYPES))
        ranked = sorted(range(len(FAULT_TYPES)), key=lambda j: (used[FAULT_TYPES[j]], ties[j]))
        types = [FAULT_TYPES[j] for j in ranked[:k]]
        scenario = SafetyScenario(
            set_name=set_name,
            index=len(out) + 1,
            seed=int(rng.integers(0, 2**31)),
            base_cut=base.manifest.cut_id,
            augment=int(rng.integers(*AUGMENT_RANGE)),
            faults=tuple(_fault(t, rng) for t in types),
        )
        try:
            plant(scenario, base)
        except FaultError:
            continue
        out.append(scenario)
        for t in types:
            used[t] += 1
    return out


@dataclass(frozen=True, slots=True)
class SafetyInjectResult:
    scenario: SafetyScenario
    ground_truth: GroundTruth
    event: Event
    cut_created: bool


class SafetyInjector:
    """Writes each scenario's cut (AE replaced), its safety database snapshot, its
    ground truth and a DataCut event, in that order and each once."""

    def __init__(
        self,
        *,
        clock: Clock,
        ledger: Ledger,
        curated_root: Path,
        safety_root: Path,
        study_id: str,
        ground_truth_dir: Path,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._curated_root = curated_root
        self._safety_root = safety_root
        self._study_id = study_id
        self._ground_truth_dir = ground_truth_dir

    def base(self, cut_id: str = BASE_CUT) -> FullCut:
        return FullCutReader(self._curated_root / self._study_id / cut_id).read()

    def inject(self, scenario: SafetyScenario, base: FullCut) -> SafetyInjectResult:
        planted = plant(scenario, base)
        cut_id = scenario.cut_id
        truth = GroundTruth(
            cut_id=cut_id,
            scenario_path=f"safety:{scenario.set_name}/{scenario.index:02d}",
            scenario_sha256=sha256_hex(scenario.canonical().encode("utf-8")),
            seed=scenario.seed,
            base_cut=scenario.base_cut,
            base_manifest_sha256=base.manifest_sha256,
            faults=planted.records,
            label=scenario.label,
        )
        truth_text = truth.dumps()
        truth_path = self._ground_truth_dir / f"{cut_id}.json"
        if truth_path.exists() and truth_path.read_text(encoding="utf-8") != truth_text:
            raise FaultError(f"{truth_path} exists with different content")
        written = write_cut(
            curated_root=self._curated_root,
            study_id=self._study_id,
            cut_id=cut_id,
            blinded={**base.blinded, "ae": planted.ae},
            ops=base.ops,
            unblinded=base.unblinded,
            created_at=self._clock.now(),
            seed=base.manifest.seed,
            seed_record={},
            sources=[],
            derived_from_cut=base.manifest.cut_id,
            derived_from_manifest_sha256=base.manifest_sha256,
        )
        cut = FullCutReader(written.path).read()
        write_snapshot(
            safety_root=self._safety_root,
            study_id=self._study_id,
            cut=cut,
            cases=planted.cases,
            harmless=planted.harmless,
            seed=planted.case_seed,
            clock=self._clock,
        )
        if not truth_path.exists():
            self._ground_truth_dir.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(truth_path, truth_text.encode("utf-8"))
        event, _ = record_data_cut(
            self._ledger,
            clock=self._clock,
            actor=FAULTS_ACTOR,
            study_id=self._study_id,
            cut_id=cut_id,
            path=written.path,
        )
        return SafetyInjectResult(scenario, truth, event, written.created)

    def inject_set(self, set_name: SetName) -> list[SafetyInjectResult]:
        base = self.base()
        return [self.inject(sc, base) for sc in scenario_set(set_name, base)]


def expected(truth: GroundTruth) -> set[tuple[str, str]]:
    """(check, record key) of every planted record."""
    return {(r.kri or "", r.target) for r in truth.faults}


def found(checks: Sequence[str]) -> set[tuple[str, str]]:
    """(check, record key) from dedup-key check parts "<check>:<record>"."""
    out = set()
    for c in checks:
        name, _, record = c.partition(":")
        out.add((name, record))
    return out

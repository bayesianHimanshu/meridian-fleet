"""TMF faults: record-level gaps planted into seeded evaluation cuts' eTMF index, with
ground truth per record.

A TMF scenario is a canonical form (domain, set, index, seed, base cut, faults); its cut
id is "DC-" plus the first 8 hex characters of sha256 of that canonical JSON, so the same
scenario always names the same cut. The cut's clinical data is the base cut's, unchanged;
only its eTMF snapshot differs. The clean index is generated with the scenario's seed and
each fault is planted on a site no other fault of the cut touches (one trial-level fault
at most), so faults never mask each other:

* tmf_missing_artifact: every version of one expected artifact is removed (trial level or
  one site; never a visit report, never Protocol Amendment, which is filed on issue).
* tmf_late_filing: the counting (latest) version of one artifact, or one visit report, is
  filed 5 to 60 days after its allowed window.
* tmf_approval_after_first_subject: every version of a site's IRB or IEC Decision or Form
  FDA 1572 dated on or before its first subject moves to 3 to 45 days after it (filed on
  time from the new date).
* tmf_misfiled: a site document is re-indexed under another site; the document still
  names its own site. That site then lacks it, so the ground truth also holds a
  tmf_missing_artifact for it (fault type tmf_misfiled).
* tmf_monitoring_report_missing: the report of one scheduled visit is removed.
* tmf_qc_overdue: a counting version filed well before the export is left pending QC.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from meridian.data.cut_writer import write_cut
from meridian.data.datacut import Ledger, record_data_cut
from meridian.data.files import write_bytes_atomic
from meridian.domain import tmf as t
from meridian.domain.enums import FindingKind
from meridian.domain.events import Event
from meridian.faults.catalog import FaultError, FaultRecord
from meridian.faults.inject import FAULTS_ACTOR, GroundTruth
from meridian.faults.reader import FullCut, FullCutReader
from meridian.ledger.hashing import canonical_json
from meridian.platform.clock import Clock
from meridian.platform.tmf_reference import ExpectedArtifact, TmfReference
from meridian.tmf.generate import Index, generate, write_snapshot

FAULT_TYPES: tuple[str, ...] = (
    t.MISSING_ARTIFACT,
    t.LATE_FILING,
    t.APPROVAL_AFTER_FIRST_SUBJECT,
    t.MISFILED,
    t.MONITORING_REPORT_MISSING,
    t.QC_OVERDUE,
)
SetName = Literal["dev", "holdout", "clean"]
SET_SEEDS: dict[str, int] = {"dev": 20261120, "holdout": 20261121, "clean": 20261122}
SET_SIZES: dict[str, int] = {"dev": 30, "holdout": 30, "clean": 10}
MAX_FAULTS = 3
BASE_CUT = "DC-BASE"
LABEL_PREFIX = "tmf_"
APPROVALS = ("irb_iec_decision", "form_fda_1572")
LATE_BY = (5, 61)
APPROVAL_AFTER = (3, 46)
QC_MARGIN_DAYS = 5


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TmfFault(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    params: dict[str, float]


class TmfScenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: Literal["tmf"] = "tmf"
    set_name: SetName
    index: int
    seed: int
    base_cut: str
    faults: tuple[TmfFault, ...]

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    @property
    def cut_id(self) -> str:
        return "DC-" + sha256_hex(self.canonical().encode("utf-8"))[:8]

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}{self.set_name}_{self.index:02d}"


@dataclass(slots=True)
class Planted:
    index: Index
    records: list[FaultRecord] = field(default_factory=list)


def _record(n: int, fault_type: str, check: str, target: str, achieved: float) -> FaultRecord:
    return FaultRecord(
        fault_id=f"tmf-{n:02d}",
        fault_type=fault_type,
        target=target,
        params={},
        expected_kind=FindingKind.TMF,
        kri=check,
        rows_touched={t.ETMF_INDEX: 1},
        achieved={"value": achieved},
    )


class _Planter:
    def __init__(self, index: Index, ref: TmfReference) -> None:
        self.index = index
        self.ref = ref
        self.by_name = ref.by_name()
        self.used: set[str] = set()  # sites (and "TRIAL") a fault already touched
        self.records: list[FaultRecord] = []

    # -- what is there

    def artifact(self, d: dict[str, str]) -> ExpectedArtifact:
        return self.by_name[t.norm_name(d[t.ARTIFACT])]

    def docs(self, site: str, key: str) -> list[dict[str, str]]:
        return [
            d for d in self.index.documents if d[t.SITE] == site and self.artifact(d).key == key
        ]

    def counting(self, site: str, key: str) -> dict[str, str] | None:
        """The latest version that is not rejected, as the agent counts it."""
        kept = [d for d in self.docs(site, key) if d[t.QC_STATUS] != t.QC_REJECTED]
        return max(kept, key=lambda d: int(d[t.VERSION])) if kept else None

    def sites(self) -> list[str]:
        return sorted({v.site for v in self.index.visits} - self.used)

    def site_first(self, site: str, dm_first: dict[str, date]) -> date | None:
        return dm_first.get(site)

    # -- faults

    def missing(self, rng: np.random.Generator, n: int) -> None:
        trial = "TRIAL" not in self.used and bool(rng.random() < 0.35)
        if trial:
            keys = [
                a.key
                for a in self.ref.artifacts
                if a.level == "trial" and a.milestone != "on_issue"
            ]
            key = keys[int(rng.integers(0, len(keys)))]
            site, record = "", f"{t.TRIAL_RECORD}/{key}"
            self.used.add("TRIAL")
        else:
            site = self._site(rng)
            keys = [a.key for a in self.ref.artifacts if a.milestone == "site_activation"]
            key = keys[int(rng.integers(0, len(keys)))]
            record = f"{site}/{key}"
        gone = self.docs(site, key)
        if not gone:
            raise FaultError(f"nothing to remove for {record}")
        self.index.documents = [d for d in self.index.documents if d not in gone]
        self.records.append(_record(n, t.MISSING_ARTIFACT, t.MISSING_ARTIFACT, record, len(gone)))

    def late(self, rng: np.random.Generator, n: int) -> None:
        site = self._site(rng)
        if bool(rng.random() < 0.5):
            visits = [v for v in self.index.visits if v.site == site]
            v = visits[int(rng.integers(0, len(visits)))]
            report = self.ref.for_visit(v.visit_type)
            assert report is not None
            candidates = [
                d
                for d in self.docs(site, report.key)
                if abs((date.fromisoformat(d[t.DOCUMENT_DATE]) - v.day).days)
                <= self.ref.rules.visit_match_days
            ]
            doc = candidates[0] if candidates else None
            a = report
        else:
            keys = [a.key for a in self.ref.artifacts if a.milestone == "site_activation"]
            key = keys[int(rng.integers(0, len(keys)))]
            doc = self.counting(site, key)
            a = self.ref.by_key()[key]
        if doc is None:
            raise FaultError(f"no document to file late at site {site}")
        day = date.fromisoformat(doc[t.DOCUMENT_DATE])
        filed = day + timedelta(days=a.file_within_days + int(rng.integers(*LATE_BY)))
        if filed > self.index.export_date - timedelta(days=self.ref.rules.qc_within_days + 1):
            raise FaultError("a late filing would land after the export")
        doc[t.FILED_DATE] = filed.isoformat()
        doc[t.QC_STATUS] = t.QC_PASSED
        self.records.append(
            _record(n, t.LATE_FILING, t.LATE_FILING, doc[t.DOCUMENT_ID], (filed - day).days)
        )

    def approval(self, rng: np.random.Generator, n: int, dm_first: dict[str, date]) -> None:
        site = self._site(rng)
        first = dm_first.get(site)
        if first is None:
            raise FaultError(f"site {site} has no first subject")
        key = APPROVALS[int(rng.integers(0, len(APPROVALS)))]
        a = self.ref.by_key()[key]
        moved = 0
        for d in self.docs(site, key):
            if date.fromisoformat(d[t.DOCUMENT_DATE]) <= first:
                new = first + timedelta(days=int(rng.integers(*APPROVAL_AFTER)))
                d[t.DOCUMENT_DATE] = new.isoformat()
                d[t.FILED_DATE] = (
                    new + timedelta(days=int(rng.integers(1, a.file_within_days)))
                ).isoformat()
                d[t.QC_STATUS] = t.QC_PASSED
                moved += 1
        if not moved:
            raise FaultError(f"no approval of site {site} to move")
        self.records.append(
            _record(
                n,
                t.APPROVAL_AFTER_FIRST_SUBJECT,
                t.APPROVAL_AFTER_FIRST_SUBJECT,
                f"{site}/{key}",
                moved,
            )
        )

    def misfiled(self, rng: np.random.Generator, n: int) -> None:
        site = self._site(rng)
        other = self._site(rng)
        keys = [a.key for a in self.ref.artifacts if a.milestone == "site_activation"]
        key = keys[int(rng.integers(0, len(keys)))]
        docs = [d for d in self.docs(site, key) if d[t.QC_STATUS] != t.QC_REJECTED]
        if len(docs) != 1:
            raise FaultError(f"{site}/{key} has {len(docs)} counting versions; need 1")
        doc = docs[0]
        doc[t.SITE] = other  # DOC_SITE still names the site the document is about
        self.records.append(_record(n, t.MISFILED, t.MISFILED, doc[t.DOCUMENT_ID], 1))
        self.records.append(_record(n, t.MISFILED, t.MISSING_ARTIFACT, f"{site}/{key}", 1))

    def report_missing(self, rng: np.random.Generator, n: int) -> None:
        site = self._site(rng)
        visits = [v for v in self.index.visits if v.site == site]
        v = visits[int(rng.integers(0, len(visits)))]
        report = self.ref.for_visit(v.visit_type)
        assert report is not None
        gone = [
            d
            for d in self.docs(site, report.key)
            if abs((date.fromisoformat(d[t.DOCUMENT_DATE]) - v.day).days)
            <= self.ref.rules.visit_match_days
        ]
        if len(gone) != 1:
            raise FaultError(f"visit {v.visit_id} has {len(gone)} reports")
        self.index.documents = [d for d in self.index.documents if d not in gone]
        self.records.append(
            _record(n, t.MONITORING_REPORT_MISSING, t.MONITORING_REPORT_MISSING, v.visit_id, 1)
        )

    def qc(self, rng: np.random.Generator, n: int) -> None:
        site = self._site(rng)
        limit = self.index.export_date - timedelta(
            days=self.ref.rules.qc_within_days + QC_MARGIN_DAYS
        )
        candidates = sorted(
            (
                d
                for d in self.index.documents
                if d[t.SITE] == site
                and d[t.QC_STATUS] == t.QC_PASSED
                and date.fromisoformat(d[t.FILED_DATE]) < limit
                and d is self.counting(site, self.artifact(d).key)
                and self.artifact(d).visit_type is None
            ),
            key=lambda d: d[t.DOCUMENT_ID],
        )
        if not candidates:
            raise FaultError(f"no document of site {site} to leave pending")
        doc = candidates[int(rng.integers(0, len(candidates)))]
        doc[t.QC_STATUS] = t.QC_PENDING
        days = (self.index.export_date - date.fromisoformat(doc[t.FILED_DATE])).days
        self.records.append(_record(n, t.QC_OVERDUE, t.QC_OVERDUE, doc[t.DOCUMENT_ID], days))

    def _site(self, rng: np.random.Generator) -> str:
        sites = self.sites()
        if not sites:
            raise FaultError("no site left for another fault")
        site = sites[int(rng.integers(0, len(sites)))]
        self.used.add(site)
        return site


def _children(seed: int, n: int) -> list[np.random.Generator]:
    return [np.random.Generator(np.random.PCG64(c)) for c in np.random.SeedSequence(seed).spawn(n)]


def plant(scenario: TmfScenario, base: FullCut, ref: TmfReference) -> Planted:
    """The scenario's eTMF index: generated clean with its seed, then its faults."""
    from meridian.tmf.milestones import site_milestones

    dm = base.blinded["dm"]
    index = generate(dm, ref, scenario.seed)
    firsts = {
        s: m.first_subject for s, m in site_milestones(dm).items() if m.first_subject is not None
    }
    p = _Planter(index, ref)
    rngs = _children(scenario.seed, len(scenario.faults) + 1)
    for i, (fault, rng) in enumerate(zip(scenario.faults, rngs[1:], strict=True), start=1):
        if fault.type == t.MISSING_ARTIFACT:
            p.missing(rng, i)
        elif fault.type == t.LATE_FILING:
            p.late(rng, i)
        elif fault.type == t.APPROVAL_AFTER_FIRST_SUBJECT:
            p.approval(rng, i, firsts)
        elif fault.type == t.MISFILED:
            p.misfiled(rng, i)
        elif fault.type == t.MONITORING_REPORT_MISSING:
            p.report_missing(rng, i)
        elif fault.type == t.QC_OVERDUE:
            p.qc(rng, i)
        else:
            raise FaultError(f"unknown TMF fault {fault.type!r}")
    # A planted change can undo a harmless variation (a removed document); keep only those
    # still in the index.
    present = {d[t.DOCUMENT_ID] for d in index.documents}
    index.harmless = {k: [i for i in v if i in present] for k, v in index.harmless.items()}
    return Planted(index=index, records=p.records)


def scenario_set(set_name: SetName, base: FullCut, ref: TmfReference) -> list[TmfScenario]:
    """The set's scenarios, drawn from its seed: 1 to MAX_FAULTS faults each (none for
    clean), using the least used types first, so every type is planted about as often."""
    rng = np.random.Generator(np.random.PCG64(SET_SEEDS[set_name]))
    used = dict.fromkeys(FAULT_TYPES, 0)
    out: list[TmfScenario] = []
    attempts = 0
    while len(out) < SET_SIZES[set_name]:
        attempts += 1
        if attempts > SET_SIZES[set_name] * 20:
            raise FaultError(f"could not draw the {set_name} set")
        k = 0 if set_name == "clean" else int(rng.integers(1, MAX_FAULTS + 1))
        ties = rng.random(len(FAULT_TYPES))
        ranked = sorted(range(len(FAULT_TYPES)), key=lambda j: (used[FAULT_TYPES[j]], ties[j]))
        types = [FAULT_TYPES[j] for j in ranked[:k]]
        scenario = TmfScenario(
            set_name=set_name,
            index=len(out) + 1,
            seed=int(rng.integers(0, 2**31)),
            base_cut=base.manifest.cut_id,
            faults=tuple(TmfFault(type=ty, params={}) for ty in types),
        )
        try:
            plant(scenario, base, ref)
        except FaultError:
            continue
        out.append(scenario)
        for ty in types:
            used[ty] += 1
    return out


@dataclass(frozen=True, slots=True)
class TmfInjectResult:
    scenario: TmfScenario
    ground_truth: GroundTruth
    event: Event
    cut_created: bool


class TmfInjector:
    """Writes each scenario's cut (the base cut's data under the scenario's cut id), its
    eTMF snapshot, its ground truth and a DataCut event, in that order and each once."""

    def __init__(
        self,
        *,
        clock: Clock,
        ledger: Ledger,
        curated_root: Path,
        etmf_root: Path,
        study_id: str,
        ground_truth_dir: Path,
        ref: TmfReference,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._curated_root = curated_root
        self._etmf_root = etmf_root
        self._study_id = study_id
        self._ground_truth_dir = ground_truth_dir
        self._ref = ref

    def base(self, cut_id: str = BASE_CUT) -> FullCut:
        return FullCutReader(self._curated_root / self._study_id / cut_id).read()

    def inject(self, scenario: TmfScenario, base: FullCut) -> TmfInjectResult:
        planted = plant(scenario, base, self._ref)
        cut_id = scenario.cut_id
        truth = GroundTruth(
            cut_id=cut_id,
            scenario_path=f"tmf:{scenario.set_name}/{scenario.index:02d}",
            scenario_sha256=sha256_hex(scenario.canonical().encode("utf-8")),
            seed=scenario.seed,
            base_cut=scenario.base_cut,
            base_manifest_sha256=base.manifest_sha256,
            faults=tuple(planted.records),
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
            blinded=base.blinded,
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
            etmf_root=self._etmf_root,
            study_id=self._study_id,
            cut=cut,
            index=planted.index,
            seed=scenario.seed,
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
        return TmfInjectResult(scenario, truth, event, written.created)

    def inject_set(self, set_name: SetName) -> list[TmfInjectResult]:
        base = self.base()
        return [self.inject(sc, base) for sc in scenario_set(set_name, base, self._ref)]


def expected(truth: GroundTruth) -> set[tuple[str, str]]:
    """(check, record key) of every planted record."""
    return {(r.kri or "", r.target) for r in truth.faults}


def set_cuts(ground_truth_dir: Path, name: str) -> list[str]:
    """Cut ids of one TMF evaluation set, by the label in their ground truth, in order."""
    out = []
    for path in sorted(ground_truth_dir.glob("DC-*.json")):
        truth = GroundTruth.load(path)
        if truth.label and truth.label.startswith(f"{LABEL_PREFIX}{name}_"):
            out.append((truth.label, truth.cut_id))
    return [cut for _, cut in sorted(out)]


def found(checks: Sequence[str]) -> set[tuple[str, str]]:
    out = set()
    for c in checks:
        name, _, record = c.partition(":")
        out.add((name, record))
    return out

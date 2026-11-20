"""MRD-101's nonclinical study inventory (phase 5 step 2): synthetic, seeded, and committed
as data/inventory/MRD-101/inventory.csv. preclinical.ind_readiness reads it, staged as
/stage/inventory (the preclinical zone only).

One entry is real: MRD-101-TOX-01, the PointCross 13-week rat study, with its dates from
its TS domain (study start STSTDTC 2016-01-15, dosing 2016-02-01 to 2016-05-16). Its report
dates, and every other study, are invented for this fictional asset: the plan is fixed
here (which studies exist, their type, species, duration and status) and the seed draws
only the dates within each study's window, so regenerating with the same seed gives the
same file byte for byte (a test checks the committed file).

The plan has the gaps a real programme at this stage might have, on purpose: the dog study
is in life (as the portfolio says), so its report and its toxicokinetics are not QA
audited by the planned submission; one safety pharmacology study has no GLP status
recorded; and no in vitro metabolism or plasma protein binding study is planned. Those are
findings for this asset, not errors.
"""

import csv
import io
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SEED = 20261119
INVENTORY_FILE = Path("data/inventory/MRD-101/inventory.csv")
REPORT_STATUSES = ("planned", "ongoing", "draft", "final", "qa_audited")
GLP_VALUES = ("Y", "N", "")


@dataclass(frozen=True, slots=True)
class InventoryStudy:
    study_id: str
    title: str
    study_type: str
    species: str
    route: str
    duration_weeks: float | None
    glp: str  # Y, N, or empty when not recorded
    report_status: str
    start_date: date | None
    report_final_date: date | None  # actual when final or QA audited, else planned
    qa_audit_date: date | None  # actual when QA audited, else planned
    part_of: str  # the study a toxicokinetic or satellite part belongs to, or empty

    def row(self) -> dict[str, str]:
        out = {}
        for k, v in asdict(self).items():
            if v is None:
                out[k] = ""
            elif isinstance(v, float):
                out[k] = f"{v:g}"
            else:
                out[k] = str(v)
        return out


COLUMNS = tuple(f.name for f in fields(InventoryStudy))


@dataclass(frozen=True, slots=True)
class _Plan:
    study_id: str
    title: str
    study_type: str
    species: str
    route: str
    duration_weeks: float | None
    glp: str
    report_status: str
    start: tuple[str, int]  # earliest start, and days the seed may add
    report_after_days: tuple[int, int] | None  # report final this many days after start
    qa_after_days: tuple[int, int] | None  # QA audit this many days after the report
    part_of: str = ""


# The real study, as recorded in its SEND TS domain; report and audit dates invented.
_TOX01 = InventoryStudy(
    study_id="MRD-101-TOX-01",
    title="13-Week Repeat Dose Toxicity Study on MRD-101 in Rats",
    study_type="repeat_dose_toxicity",
    species="RAT",
    route="ORAL GAVAGE",
    duration_weeks=13.0,
    glp="Y",
    report_status="qa_audited",
    start_date=date(2016, 1, 15),
    report_final_date=date(2016, 9, 30),
    qa_audit_date=date(2016, 10, 28),
    part_of="",
)

_PLAN: tuple[_Plan, ...] = (
    _Plan(
        "MRD-101-TOX-01-TK",
        "Toxicokinetics of the 13-week rat study (satellite groups)",
        "toxicokinetics",
        "RAT",
        "ORAL GAVAGE",
        13.0,
        "Y",
        "qa_audited",
        ("2016-01-15", 0),
        (240, 30),
        (14, 21),
        part_of="MRD-101-TOX-01",
    ),
    _Plan(
        "MRD-101-DRF-01",
        "4-week dose range-finding study in rats",
        "repeat_dose_toxicity",
        "RAT",
        "ORAL GAVAGE",
        4.0,
        "N",
        "final",
        ("2015-09-01", 30),
        (90, 30),
        None,
    ),
    _Plan(
        "MRD-101-DRF-02",
        "2-week maximum tolerated dose study in dogs",
        "repeat_dose_toxicity",
        "DOG",
        "ORAL CAPSULE",
        2.0,
        "N",
        "final",
        ("2026-03-02", 21),
        (60, 20),
        None,
    ),
    _Plan(
        "MRD-101-TOX-02",
        "13-week repeat dose toxicity study in beagle dogs with a 4-week recovery",
        "repeat_dose_toxicity",
        "DOG",
        "ORAL CAPSULE",
        13.0,
        "Y",
        "ongoing",
        ("2026-09-07", 14),
        (240, 21),
        (21, 14),
    ),
    _Plan(
        "MRD-101-TOX-02-TK",
        "Toxicokinetics of the 13-week dog study",
        "toxicokinetics",
        "DOG",
        "ORAL CAPSULE",
        13.0,
        "Y",
        "ongoing",
        ("2026-09-07", 14),
        (240, 21),
        (21, 14),
        part_of="MRD-101-TOX-02",
    ),
    _Plan(
        "MRD-101-SP-01",
        "Functional observational battery in rats (CNS)",
        "safety_pharmacology_cns",
        "RAT",
        "ORAL GAVAGE",
        None,
        "Y",
        "qa_audited",
        ("2026-02-02", 28),
        (70, 21),
        (14, 14),
    ),
    _Plan(
        "MRD-101-SP-02",
        "Cardiovascular telemetry in conscious beagle dogs",
        "safety_pharmacology_cardiovascular",
        "DOG",
        "ORAL CAPSULE",
        None,
        "Y",
        "draft",
        ("2026-07-06", 21),
        (150, 21),
        (30, 14),
    ),
    _Plan(
        "MRD-101-SP-03",
        "Respiratory function by whole-body plethysmography in rats",
        "safety_pharmacology_respiratory",
        "RAT",
        "ORAL GAVAGE",
        None,
        "",
        "qa_audited",
        ("2026-02-16", 28),
        (70, 21),
        (14, 14),
    ),
    _Plan(
        "MRD-101-SP-04",
        "hERG (IKr) patch-clamp assay in HEK293 cells",
        "in_vitro_ikr",
        "IN VITRO",
        "IN VITRO",
        None,
        "Y",
        "qa_audited",
        ("2025-11-03", 21),
        (45, 14),
        (14, 7),
    ),
    _Plan(
        "MRD-101-GT-01",
        "Bacterial reverse mutation assay (Ames)",
        "genotoxicity_bacterial_mutation",
        "IN VITRO",
        "IN VITRO",
        None,
        "Y",
        "qa_audited",
        ("2025-10-06", 21),
        (45, 14),
        (14, 7),
    ),
    _Plan(
        "MRD-101-GT-02",
        "In vitro micronucleus assay in human peripheral blood lymphocytes",
        "genotoxicity_in_vitro_chromosomal",
        "IN VITRO",
        "IN VITRO",
        None,
        "Y",
        "final",
        ("2026-07-06", 14),
        (75, 14),
        (60, 14),
    ),
)


def _when(rng: np.random.Generator, base: date, window: tuple[int, int]) -> date:
    low, spread = window
    return base + timedelta(days=low + int(rng.integers(0, spread + 1)))


def generate_inventory(seed: int = SEED) -> list[InventoryStudy]:
    """The inventory: the real study, then each planned study with its dates drawn in
    order from numpy's default_rng(seed)."""
    rng = np.random.default_rng(seed)
    out = [_TOX01]
    for p in _PLAN:
        start = date.fromisoformat(p.start[0]) + timedelta(
            days=int(rng.integers(0, p.start[1] + 1))
        )
        if p.part_of == _TOX01.study_id:
            start = _TOX01.start_date or start
        report = _when(rng, start, p.report_after_days) if p.report_after_days else None
        qa = _when(rng, report, p.qa_after_days) if report and p.qa_after_days else None
        out.append(
            InventoryStudy(
                study_id=p.study_id,
                title=p.title,
                study_type=p.study_type,
                species=p.species,
                route=p.route,
                duration_weeks=p.duration_weeks,
                glp=p.glp,
                report_status=p.report_status,
                start_date=start,
                report_final_date=report,
                qa_audit_date=qa,
                part_of=p.part_of,
            )
        )
    return out


def inventory_csv(studies: list[InventoryStudy]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for s in studies:
        writer.writerow(s.row())
    return buf.getvalue()

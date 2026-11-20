"""The translational bridge's rules (phase 5 step 3a), as pre-registered in
docs/translational-notes.md: the NOAEL's target organs mapped to Phase 1 monitoring
(config/translational_monitoring.toml) and the safety margins of the NOAEL and LOAEL
against the planned starting dose. Pure; preclinical.translational_bridge words the result
as a finding and the API shows it."""

from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

from meridian.platform.agent_settings import (
    DoseTranslationConfig,
    OrganMonitoring,
    TranslationalMapping,
)
from meridian.preclinical.dose import NoCitedFactorError, working


@dataclass(frozen=True, slots=True)
class CitedFinding:
    finding_id: str
    check: str
    title: str
    cited_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Proposal:
    finding_id: str
    seq: int
    check: str
    status: str
    decided_by: str
    starting_dose_mg: float | None
    mrsd_mg_per_kg: float | None
    human_weight_kg: float | None
    basis_decision_id: str


def cited_findings(frame: pl.DataFrame) -> list[CitedFinding]:
    return [
        CitedFinding(
            finding_id=str(r["finding_id"]),
            check=str(r["check"]),
            title=str(r["title"]),
            cited_by=tuple(x for x in str(r["cited_by"] or "").split(",") if x),
        )
        for r in frame.sort("finding_id").iter_rows(named=True)
    ]


def current_proposal(frame: pl.DataFrame) -> Proposal | None:
    if not frame.height:
        return None
    r = frame.sort("seq").row(-1, named=True)

    def num(key: str) -> float | None:
        v = r[key]
        return None if v is None else float(v)

    return Proposal(
        finding_id=str(r["finding_id"]),
        seq=int(r["seq"]),
        check=str(r["check"]),
        status=str(r["status"]),
        decided_by=str(r["decided_by"] or ""),
        starting_dose_mg=num("starting_dose_mg"),
        mrsd_mg_per_kg=num("mrsd_mg_per_kg"),
        human_weight_kg=num("human_weight_kg"),
        basis_decision_id=str(r["basis_decision_id"] or ""),
    )


def organ_of(check: str, mapping: TranslationalMapping) -> OrganMonitoring | None:
    """The first mapping entry (in its order) one of whose prefixes starts the check, as
    written or without its first part (the rule name)."""
    forms = (check, check.split(":", 1)[1] if ":" in check else check)
    for organ in mapping.organs:
        if any(form.startswith(prefix) for prefix in organ.matches for form in forms):
            return organ
    return None


@dataclass(frozen=True, slots=True)
class Item:
    organ: OrganMonitoring
    findings: tuple[CitedFinding, ...]


@dataclass(frozen=True, slots=True)
class Margin:
    """A dose of the basis decision as a human equivalent dose and its ratio to the
    planned starting dose (None without a starting dose)."""

    label: str  # "NOAEL" or "LOAEL"
    animal_dose: float
    dose_unit: str
    hed_mg_per_kg: float
    hed_mg: float
    ratio: float | None


@dataclass(frozen=True, slots=True)
class Recommendation:
    items: tuple[Item, ...]
    unmapped: tuple[CitedFinding, ...]
    margins: tuple[Margin, ...]
    margin_error: str | None  # why no margin could be computed (an uncited species)
    starting_dose_mg: float | None
    human_weight_kg: float


def recommend(
    findings: Sequence[CitedFinding],
    noael: float | None,
    loael: float | None,
    dose_unit: str,
    species: str,
    proposal: Proposal | None,
    mapping: TranslationalMapping,
    dose: DoseTranslationConfig,
) -> Recommendation:
    by_organ: dict[str, list[CitedFinding]] = {}
    unmapped: list[CitedFinding] = []
    for f in findings:
        organ = organ_of(f.check, mapping)
        if organ is None:
            unmapped.append(f)
        else:
            by_organ.setdefault(organ.id, []).append(f)
    items = tuple(Item(o, tuple(by_organ[o.id])) for o in mapping.organs if o.id in by_organ)
    usable = (
        proposal is not None
        and proposal.check == "dose_proposal"
        and proposal.starting_dose_mg is not None
    )
    start = proposal.starting_dose_mg if usable and proposal is not None else None
    weight = (
        proposal.human_weight_kg
        if usable and proposal is not None and proposal.human_weight_kg
        else dose.human_weight_kg
    )
    margins: list[Margin] = []
    error = None
    for label, value in (("NOAEL", noael), ("LOAEL", loael)):
        if value is None:
            continue
        try:
            hed = working(value, dose_unit, species, dose).hed_mg_per_kg
        except (NoCitedFactorError, ValueError) as exc:
            error = str(exc)
            break
        mg = hed * weight
        margins.append(
            Margin(label, value, dose_unit, hed, mg, None if start is None else mg / start)
        )
    return Recommendation(items, tuple(unmapped), tuple(margins), error, start, weight)

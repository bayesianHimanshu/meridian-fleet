"""preclinical.dose_translation 1.0.0: from the toxicologist's NOAEL to a proposed maximum
recommended starting dose (MRSD) for MRD-101's first-in-human study, by FDA's 2005
guidance (meridian.preclinical.dose; docs/fih-dose-notes.md).

It wakes on a NoaelDecision. Its inputs are the study's NOAEL decisions up to that one
(staged as /stage/noael by the host, which alone reads the ledger), the study's design
from SEND (TS species and route, TX dose levels) and, when SEND has them, the
toxicokinetic domains PC and PP. It uses the current decision of the combined sexes when
one is recorded, else the most sensitive sex's ("not established" first, then the lower
NOAEL). No decision: it records nothing. "Not established": one finding saying no starting
dose can be derived. Otherwise one DoseProposal finding with every step as evidence, its
formula and its source, and its limitations.

It proposes; it never decides. The medical monitor accepts, escalates or dismisses the
proposal, and the toxicologist who made the NOAEL may not (the proposal names them as the
actor under review: the self-decision rule). A revised NOAEL wakes the agent again and its
new proposal supersedes this one (meridian.api.repository).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.data.study_store import MissingTableError, StudyReader
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, NoaelDecision
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import DoseTranslationConfig
from meridian.preclinical.dose import NoCitedFactorError, Working, sig, working
from meridian.preclinical.noael_stage import NOAEL_TABLE

AGENT_ID = "preclinical.dose_translation"
VERSION = "1.0.0"
VISIBLE_TO_MAX = frozenset(
    {Role.TOXICOLOGIST, Role.MEDICAL_MONITOR, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR}
)
PROPOSAL = "dose_proposal"
NO_DOSE = "no_starting_dose"
SEX_TEXT = {"M": "males", "F": "females", "combined": "both sexes"}
DECIDES = (
    " This is a proposal, not a decision: the medical monitor accepts, escalates or "
    "dismisses it, and the toxicologist who recorded the NOAEL may not."
)


@dataclass(frozen=True, slots=True)
class StagedDecision:
    """A NoaelDecision as the host stages it (meridian.preclinical.noael_extract)."""

    seq: int
    decision_id: str
    study_id: str
    sex: str
    established: bool
    noael_dose: float | None
    loael_dose: float | None
    dose_unit: str
    decided_by: str
    supersedes: str | None
    recorded_at: str


def staged_decisions(frame: pl.DataFrame) -> list[StagedDecision]:
    out = []
    for r in frame.sort("seq").iter_rows(named=True):
        out.append(
            StagedDecision(
                seq=int(r["seq"]),
                decision_id=str(r["decision_id"]),
                study_id=str(r["study_id"]),
                sex=str(r["sex"]),
                established=bool(r["established"]),
                noael_dose=None if r["noael_dose"] is None else float(r["noael_dose"]),
                loael_dose=None if r["loael_dose"] is None else float(r["loael_dose"]),
                dose_unit=str(r["dose_unit"]),
                decided_by=str(r["decided_by"]),
                supersedes=None if r["supersedes"] in (None, "") else str(r["supersedes"]),
                recorded_at=str(r["recorded_at"]),
            )
        )
    return out


def current_decisions(decisions: Sequence[StagedDecision]) -> list[StagedDecision]:
    """The decisions nothing supersedes, one per sex (the latest if, against the API's
    rule, two were current)."""
    superseded = {d.supersedes for d in decisions if d.supersedes}
    by_sex: dict[str, StagedDecision] = {}
    for d in sorted(decisions, key=lambda d: d.seq):
        if d.decision_id not in superseded:
            by_sex[d.sex] = d
    return [by_sex[s] for s in ("M", "F", "combined") if s in by_sex]


def basis_of(current: Sequence[StagedDecision]) -> StagedDecision | None:
    """The combined decision if recorded, else the most sensitive sex's: "not
    established" first, then the lower NOAEL; equal NOAELs: males (a fixed order)."""
    combined = [d for d in current if d.sex == "combined"]
    if combined:
        return combined[0]
    sexes = [d for d in current if d.sex in ("M", "F")]
    if not sexes:
        return None
    return min(
        sexes,
        key=lambda d: (
            d.established,
            d.noael_dose if d.noael_dose is not None else 0.0,
            d.sex != "M",
        ),
    )


@dataclass(frozen=True, slots=True)
class StudyDesign:
    species: str
    strain: str
    route: str
    duration: str
    doses: tuple[float, ...]
    unit: str


def _table(study: StudyReader, name: str) -> pl.DataFrame | None:
    try:
        return study.table(name)
    except MissingTableError:
        return None


def study_design(study: StudyReader) -> StudyDesign:
    ts = _table(study, "ts")
    tx = _table(study, "tx")

    def param(code: str) -> str:
        if ts is None:
            return ""
        rows = ts.filter(pl.col("TSPARMCD") == code)["TSVAL"].to_list()
        return str(rows[0]).strip() if rows else ""

    doses: set[float] = set()
    units: set[str] = set()
    if tx is not None:
        for r in tx.filter(pl.col("TXPARMCD").is_in(["TRTDOS", "TRTDOSU"])).iter_rows(named=True):
            if r["TXPARMCD"] == "TRTDOS":
                try:
                    doses.add(float(r["TXVAL"]))
                except TypeError, ValueError:
                    continue
            else:
                units.add(str(r["TXVAL"]).strip())
    return StudyDesign(
        species=param("SPECIES"),
        strain=param("STRAIN"),
        route=param("ROUTE"),
        duration=param("DOSDUR"),
        doses=tuple(sorted(doses)),
        unit=next(iter(units)) if len(units) == 1 else "",
    )


@dataclass(frozen=True, slots=True)
class Exposure:
    test: str
    day: str
    mean: float
    n: int
    unit: str


def exposure_at(study: StudyReader, dose: float) -> list[Exposure] | None:
    """AUC and Cmax from PP for the animals whose set has the given dose (TK sets
    included), per test and day; None when the study has no PP domain."""
    pp = _table(study, "pp")
    dm = _table(study, "dm")
    tx = _table(study, "tx")
    if pp is None or dm is None or tx is None or "USUBJID" not in pp.columns:
        return None
    sets = (
        tx.filter(pl.col("TXPARMCD") == "TRTDOS")
        .with_columns(pl.col("TXVAL").cast(pl.Float64, strict=False).alias("dose"))
        .filter(pl.col("dose") == dose)["SETCD"]
        .to_list()
    )
    animals = dm.filter(pl.col("SETCD").is_in(sets))["USUBJID"].to_list()
    day_col = next((c for c in ("PPDY", "VISITDY", "PPNOMDY") if c in pp.columns), None)
    rows = pp.filter(
        pl.col("USUBJID").is_in(animals)
        & pl.col("PPTESTCD").str.to_uppercase().str.contains(r"^(AUC|CMAX)")
        & pl.col("PPSTRESN").is_not_null()
    )
    if not rows.height:
        return []
    keys = ["PPTESTCD", *([day_col] if day_col else [])]
    unit = "PPSTRESU" if "PPSTRESU" in pp.columns else None
    out = []
    for part in rows.partition_by(keys, maintain_order=True):
        values = part["PPSTRESN"].cast(pl.Float64)
        out.append(
            Exposure(
                test=str(part["PPTESTCD"][0]),
                day=str(part[day_col][0]) if day_col else "",
                mean=float(values.mean() or 0.0),  # type: ignore[arg-type]
                n=part.height,
                unit=str(part[unit][0]) if unit else "",
            )
        )
    return sorted(out, key=lambda e: (e.test, e.day))


def _decision_selector(d: StagedDecision) -> str:
    return f"DECISION={d.decision_id}; SEX={d.sex}; SEQ={d.seq}"


def _decision_text(d: StagedDecision) -> str:
    noael = f"NOAEL {d.noael_dose:g} {d.dose_unit}" if d.established else "NOAEL not established"
    loael = f", LOAEL {d.loael_dose:g} {d.dose_unit}" if d.loael_dose is not None else ""
    return (
        f"{SEX_TEXT[d.sex]}: {noael}{loael} ({d.decision_id}, by {d.decided_by}, ledger seq "
        f"{d.seq})"
    )


def _considered(current: Sequence[StagedDecision], basis: StagedDecision) -> list[Evidence]:
    return [
        Evidence(
            source="ledger:noael_decision",
            selector=_decision_selector(d),
            statistic="mrsd:decision_considered",
            value=d.noael_dose if d.noael_dose is not None else 0.0,
            role="supporting",
            direction="none",
            display=f"Current decision, {_decision_text(d)}"
            + (" (used)" if d.decision_id == basis.decision_id else " (not used)"),
        )
        for d in current
    ]


def _design_evidence(design: StudyDesign) -> Evidence:
    return Evidence(
        source="send:ts",
        selector="TSPARMCD=SPECIES,STRAIN,ROUTE,DOSDUR; TXPARMCD=TRTDOS",
        statistic="mrsd:study_design",
        value=float(len(design.doses)),
        role="supporting",
        direction="none",
        display=(
            f"Study design (TS, TX): {design.species.lower()} ({design.strain.lower()}), "
            f"{design.route.lower()}, dosing {design.duration}; dose levels "
            + ", ".join(f"{d:g}" for d in design.doses)
            + f" {design.unit}"
        ),
    )


def _steps(w: Working, basis: StagedDecision, cfg: DoseTranslationConfig) -> list[Evidence]:
    c = w.conversion
    g = "FDA 2005"
    return [
        Evidence(
            source="ledger:noael_decision",
            selector=_decision_selector(basis),
            statistic="mrsd:noael_mg_per_kg",
            value=w.noael_mg_per_kg,
            role="primary",
            direction="none",
            display=(
                f"Step 1, NOAEL: {w.noael_mg_per_kg:g} mg/kg, the current decision for "
                f"{_decision_text(basis)}. {g} section IV: the NOAEL is the starting point."
            ),
        ),
        Evidence(
            source="config:agents.dose_translation.conversions",
            selector=f"SPECIES={w.species}",
            statistic="mrsd:hed_mg_per_kg",
            value=w.hed_mg_per_kg,
            threshold=c.divide_by,
            role="primary",
            direction="none",
            display=(
                f"Step 2, human equivalent dose: HED = NOAEL / {c.divide_by:g} = "
                f"{w.noael_mg_per_kg:g} / {c.divide_by:g} = {sig(w.hed_mg_per_kg)} mg/kg. "
                f"{g} {c.citation}."
            ),
        ),
        Evidence(
            source="config:agents.dose_translation.conversions",
            selector=f"SPECIES={w.species}",
            statistic="mrsd:hed_cross_check_mg_per_kg",
            value=w.hed_by_multiplier_mg_per_kg,
            threshold=c.multiply_by,
            role="supporting",
            direction="none",
            display=(
                f"Cross-check, Table 1's other column: NOAEL x {c.multiply_by:g} = "
                f"{sig(w.hed_by_multiplier_mg_per_kg)} mg/kg; and by km, NOAEL x "
                f"{c.km:g} / {cfg.human_km:g} = "
                f"{sig(w.noael_mg_per_kg * c.km / cfg.human_km)} mg/kg ({g} "
                f"{cfg.human_km_citation}). Not used: the divisor is."
            ),
        ),
        Evidence(
            source="send:ts",
            selector=f"SPECIES={w.species}",
            statistic="mrsd:most_appropriate_species",
            value=w.hed_mg_per_kg,
            role="supporting",
            direction="none",
            display=(
                f"Step 3, most appropriate species: {w.species.lower()}, the only species with a "
                f"NOAEL. {g} section VI: by default the species with the lowest HED, chosen "
                "from all the toxicology studies relevant to the trial; here there is one."
            ),
        ),
        Evidence(
            source="config:agents.dose_translation",
            selector="safety_factor",
            statistic="mrsd:mrsd_mg_per_kg",
            value=w.mrsd_mg_per_kg,
            threshold=w.safety_factor,
            role="primary",
            direction="none",
            display=(
                f"Step 4, MRSD = HED / safety factor = {sig(w.hed_mg_per_kg)} / "
                f"{w.safety_factor:g} = {sig(w.mrsd_mg_per_kg)} mg/kg. {g} "
                f"{cfg.safety_factor_citation}"
            ),
        ),
        Evidence(
            source="config:agents.dose_translation",
            selector="human_weight_kg",
            statistic="mrsd:starting_dose_mg",
            value=w.starting_dose_mg,
            threshold=w.human_weight_kg,
            role="primary",
            direction="none",
            display=(
                f"Step 5, starting dose for a {w.human_weight_kg:g} kg adult = MRSD x "
                f"{w.human_weight_kg:g} = {sig(w.mrsd_mg_per_kg)} x {w.human_weight_kg:g} = "
                f"{sig(w.starting_dose_mg)} mg. {g} {cfg.human_weight_citation}"
            ),
        ),
    ]


def _exposure_evidence(exposure: list[Exposure] | None, dose: float, unit: str) -> Evidence:
    if exposure is None or not exposure:
        what = "no PC or PP domain" if exposure is None else "no AUC or Cmax at that dose"
        return Evidence(
            source="send:pp",
            selector=f"DOSE={dose:g} {unit}",
            statistic="mrsd:exposure_information_only",
            value=0.0,
            role="supporting",
            direction="none",
            display=(
                f"Exposure at the NOAEL: none in the SEND study ({what}), so no AUC or Cmax "
                "is shown. Information only in any case: the guidance's dose-based method "
                "is the one used."
            ),
        )
    parts = "; ".join(
        f"{e.test}{f' day {e.day}' if e.day else ''} {sig(e.mean)} {e.unit} (n = {e.n})"
        for e in exposure
    )
    return Evidence(
        source="send:pp",
        selector=f"DOSE={dose:g} {unit}",
        statistic="mrsd:exposure_information_only",
        value=exposure[0].mean,
        role="supporting",
        direction="none",
        display=(
            f"Information only, not used in the working: exposure at the NOAEL dose from PP: "
            f'{parts}. FDA 2005 section IV: exposure (AUC or Cmax) "cannot be employed for '
            'setting a safe starting dose in humans"; the dose-based method stays primary.'
        ),
    )


LIMITATIONS = (
    "One species only: the guidance derives the MRSD from the most appropriate species, "
    "chosen among all relevant toxicology studies and normally including a non-rodent "
    "(sections V.A and VI); MRD-101 has a NOAEL in the rat alone, so the dog study may "
    "lower it.",
    "No pharmacological activity data: no pharmacologically active dose (section VIII) and "
    "no MABEL comparison, so nothing shows whether the MRSD is above an active dose.",
)


def _limitation_evidence(cfg: DoseTranslationConfig) -> list[Evidence]:
    out = [
        Evidence(
            source="config:agents.dose_translation",
            selector=f"LIMITATION={i}",
            statistic="mrsd:limitation",
            value=0.0,
            role="supporting",
            direction="none",
            display=f"Limitation: {text}",
        )
        for i, text in enumerate(LIMITATIONS, start=1)
    ]
    out.append(
        Evidence(
            source="config:agents.dose_translation",
            selector="safety_factor",
            statistic="mrsd:safety_factor_used",
            value=cfg.safety_factor,
            role="supporting",
            direction="none",
            display=(
                f"Safety factor used: {cfg.safety_factor:g}, the default. FDA 2005 section VII "
                "says when to raise it (A) or lower it (B); that judgement is the reviewer's."
            ),
        )
    )
    return out


class DoseTranslation:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PRECLINICAL
    subscribes_to = frozenset({EventType.NOAEL_DECISION})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = DoseTranslationConfig

    def run(
        self, trigger: Event, ctx: AgentContext[DoseTranslationConfig]
    ) -> Sequence[FindingDraft]:
        if not isinstance(trigger.payload, NoaelDecision):
            raise ValueError(f"{AGENT_ID} wakes on a NOAEL decision, not {trigger.payload.type}")
        study, cfg = ctx.study, ctx.config
        if trigger.payload.study_id != study.study_id:
            raise ValueError(
                f"trigger is for study {trigger.payload.study_id}, the reader for {study.study_id}"
            )
        frame = _table(study, NOAEL_TABLE)
        decisions = [
            d
            for d in (staged_decisions(frame) if frame is not None else [])
            if d.study_id == study.study_id and d.seq <= trigger.seq
        ]
        current = current_decisions(decisions)
        basis = basis_of(current)
        if basis is None:
            ctx.log.info("dose.no_decision", decisions=len(decisions))
            return []
        design = study_design(study)
        draft = self.draft(basis, current, design, study, cfg)
        ctx.log.info("dose.proposed", basis=basis.decision_id, check=draft.dedup_key)
        return [draft]

    def draft(
        self,
        basis: StagedDecision,
        current: Sequence[StagedDecision],
        design: StudyDesign,
        study: StudyReader,
        cfg: DoseTranslationConfig,
    ) -> FindingDraft:
        considered = _considered(current, basis)
        if not basis.established:
            title = "No starting dose can be derived: the NOAEL is not established"
            summary = (
                f"The current NOAEL decision ({_decision_text(basis)}) finds adverse effects at "
                "every dose, so there is no NOAEL to convert. The FDA 2005 method starts from "
                "the NOAEL (section IV) and gives no starting dose without one. A lower dose "
                "level, another study, or another approach is needed." + DECIDES
            )
            evidence = [
                Evidence(
                    source="ledger:noael_decision",
                    selector=_decision_selector(basis),
                    statistic="mrsd:noael_not_established",
                    value=basis.loael_dose if basis.loael_dose is not None else 0.0,
                    role="primary",
                    direction="none",
                    display=f"Step 1, NOAEL: not established ({_decision_text(basis)}). FDA "
                    "2005 section IV: the NOAEL is the starting point; there is none.",
                ),
                *considered,
                _design_evidence(design),
            ]
            return self._draft(cfg, study, NO_DOSE, basis, title, summary, evidence, Severity.HIGH)
        assert basis.noael_dose is not None
        try:
            w = working(basis.noael_dose, basis.dose_unit, design.species, cfg)
        except (NoCitedFactorError, ValueError) as exc:
            title = "No starting dose can be derived: no cited conversion for this study"
            summary = (
                f"The NOAEL ({_decision_text(basis)}) cannot be converted: {exc}. No factor "
                "is used without a citation." + DECIDES
            )
            evidence = [
                Evidence(
                    source="ledger:noael_decision",
                    selector=_decision_selector(basis),
                    statistic="mrsd:noael_mg_per_kg",
                    value=basis.noael_dose,
                    role="primary",
                    direction="none",
                    display=f"Step 1, NOAEL: {_decision_text(basis)}; not converted: {exc}.",
                ),
                *considered,
                _design_evidence(design),
            ]
            return self._draft(cfg, study, NO_DOSE, basis, title, summary, evidence, Severity.HIGH)
        exposure = _exposure_evidence(
            exposure_at(study, basis.noael_dose), basis.noael_dose, basis.dose_unit
        )
        title = (
            # 1.0.0's recorded wording names MRD-110 by mistake (MRD-110 is a separate asset);
            # kept so that 1.0.0's drafts stay what they were. The API shows a title derived
            # at read time (phase 5 step 3a); a new version will correct the wording.
            f"Proposed MRD-110 starting dose: {sig(w.starting_dose_mg)} mg for a "
            f"{w.human_weight_kg:g} kg adult (MRSD {sig(w.mrsd_mg_per_kg)} mg/kg)"
        )
        summary = (
            f"From the {SEX_TEXT[basis.sex]} NOAEL of {w.noael_mg_per_kg:g} mg/kg in the "
            f"{w.species.lower()} ({basis.decision_id}, ledger seq {basis.seq}): HED = "
            f"{w.noael_mg_per_kg:g} / {w.conversion.divide_by:g} = {sig(w.hed_mg_per_kg)} mg/kg; "
            f"MRSD = HED / {w.safety_factor:g} = {sig(w.mrsd_mg_per_kg)} mg/kg; for a "
            f"{w.human_weight_kg:g} kg adult, {sig(w.starting_dose_mg)} mg. "
            f"Method: {cfg.guidance}. "
            "Limitations: " + " ".join(LIMITATIONS) + DECIDES
        )
        evidence = [
            *_steps(w, basis, cfg),
            *considered,
            _design_evidence(design),
            exposure,
            *_limitation_evidence(cfg),
        ]
        return self._draft(cfg, study, PROPOSAL, basis, title, summary, evidence, Severity.MEDIUM)

    def _draft(
        self,
        cfg: DoseTranslationConfig,
        study: StudyReader,
        check: str,
        basis: StagedDecision,
        title: str,
        summary: str,
        evidence: list[Evidence],
        severity: Severity,
    ) -> FindingDraft:
        return FindingDraft(
            dedup_key=make_dedup_key(
                agent_id=AGENT_ID,
                agent_version=VERSION,
                kind=FindingKind.DOSE_PROPOSAL,
                study_id=study.study_id,
                site_id=None,
                cut_id=study.cut_id,
                check=f"{check}:{basis.decision_id}",
            ),
            kind=FindingKind.DOSE_PROPOSAL,
            site_id=None,
            severity=severity,
            title=title,
            summary=summary,
            evidence=tuple(evidence),
            score=1.0 if check == NO_DOSE else 0.5,
            visible_to=cfg.visible_to,
            # The proposal rests on this person's decision: they may not decide it.
            actor_under_review=basis.decided_by,
        )


__all__ = ["AGENT_ID", "NOAEL_TABLE", "VERSION", "DoseTranslation", "basis_of", "current_decisions"]

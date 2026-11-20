"""Configuration models of the agents. Kept outside meridian.agents so that settings and
the eval harness can read thresholds without importing any agent code."""

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.domain.enums import Role
from meridian.platform.tmf_reference import TmfReference, load_tmf_reference
from meridian.portfolio.gates import Gates, load_gates


class AgentConfig(BaseModel):
    """Base for each agent's own configuration model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class KriMonitorConfig(AgentConfig):
    alpha: float = Field(0.01, gt=0, lt=1)
    lag_days: float = Field(7.0, gt=0)
    query_age_days: float = Field(21.0, gt=0)
    min_subjects: int = Field(5, ge=1)
    min_open_queries: int = Field(5, ge=1)
    # Expected-AE variant (meridian.stats.ae_expectation): a, b, c, d or e.
    ae_variant: Literal["a", "b", "c", "d", "e"] = "a"
    early_window_days: int = Field(42, ge=1)


class QtlDetectorConfig(AgentConfig):
    discontinuation_limit: float = Field(gt=0, le=1)
    approach_band: float = Field(0.02, ge=0, lt=1)


class CsmConfig(AgentConfig):
    z_threshold: float = Field(3.5, gt=0)
    sd_ratio_max: float = Field(0.6, gt=0)
    min_subjects: int = Field(5, ge=1)
    min_readings: int = Field(50, ge=1)
    parameters: tuple[str, ...] = ("SYSBP", "DIABP", "PULSE")


# The environment variable that names the config file (meridian.platform.config reads the
# same one); the safety terminology table sits next to that file.
CONFIG_FILE_ENV = "MERIDIAN_CONFIG_FILE"
SAFETY_TERMS_FILE = "safety_terms.toml"
BINARY_CAUSALITY = ("NOT RELATED", "RELATED")


def _norm(value: str) -> str:
    return " ".join(value.split()).upper()


class SafetyTerms(BaseModel):
    """Controlled terminology for outcome and causality (config/safety_terms.toml): each
    canonical value with its synonyms, and the binary class of each canonical causality."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: dict[str, tuple[str, ...]]
    causality: dict[str, tuple[str, ...]]
    causality_binary: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _consistent(self) -> SafetyTerms:
        for name, table in (("outcome", self.outcome), ("causality", self.causality)):
            seen: dict[str, str] = {}
            for canonical, synonyms in table.items():
                for value in (canonical, *synonyms):
                    key = _norm(value)
                    if seen.setdefault(key, canonical) != canonical:
                        raise ValueError(f"{name}: {value!r} maps to two canonical values")
        if set(self.causality_binary) != set(BINARY_CAUSALITY):
            raise ValueError(f"causality_binary must have exactly {BINARY_CAUSALITY}")
        classified = [v for vs in self.causality_binary.values() for v in vs]
        if sorted(classified) != sorted(self.causality) or len(set(classified)) != len(classified):
            raise ValueError("each canonical causality needs exactly one binary class")
        return self

    def outcome_of(self, value: str) -> str | None:
        """The canonical outcome; "" for not recorded; None for an unknown value."""
        return _lookup(self.outcome, value)

    def causality_of(self, value: str) -> str | None:
        """The canonical causality; "" for not recorded; None for an unknown value."""
        return _lookup(self.causality, value)

    def causality_class(self, canonical: str) -> str:
        return next(k for k, vs in self.causality_binary.items() if canonical in vs)


def _lookup(table: dict[str, tuple[str, ...]], value: str) -> str | None:
    key = _norm(value)
    if not key:
        return ""
    for canonical, synonyms in table.items():
        if key == _norm(canonical) or key in {_norm(v) for v in synonyms}:
            return canonical
    return None


def safety_terms_path() -> Path:
    """config/safety_terms.toml next to the config file in use (in the agent image:
    /opt/meridian/config/)."""
    config = os.environ.get(CONFIG_FILE_ENV)
    base = Path(config).parent if config else Path("config")
    return base / SAFETY_TERMS_FILE


def load_safety_terms(path: Path | None = None) -> SafetyTerms:
    with (path or safety_terms_path()).open("rb") as handle:
        return SafetyTerms.model_validate(tomllib.load(handle))


class SaeReconcilerConfig(AgentConfig):
    """safety.sae_reconciler: the matching rule, the pairing cost, the reporting window,
    the terminology, and who may see which finding. Visibility lives here, next to the
    agent, not in the API."""

    # Onsets more than this many days apart are an onset mismatch.
    onset_tolerance_days: int = Field(1, ge=0)
    # Pairing: a clinical SAE and a case of the same subject and preferred term may pair
    # when their onsets are at most this many days apart; within it, the cost decides.
    fallback_window_days: int = Field(7, ge=0)
    # Pairing cost (integers, so ties are exact): per day between onsets, and for an
    # outcome or a causality that differs. Lower is better; see the agent's docstring.
    cost_per_day: int = Field(1, ge=1)
    cost_outcome: int = Field(3, ge=0)
    cost_causality: int = Field(1, ge=0)
    # Recoded term: a lone unmatched SAE and a lone unmatched case of one subject with
    # different preferred terms and onsets at most this many days apart.
    term_mismatch_days: int = Field(1, ge=0)
    # A case received more than this many hours after site awareness is late (measured on
    # the case's initial version).
    late_after_hours: float = Field(24.0, gt=0)
    # A site with at least this many late cases gets one site-level finding.
    site_late_min: int = Field(2, ge=1)
    # Outcome and causality terminology (config/safety_terms.toml).
    terms: SafetyTerms = Field(default_factory=load_safety_terms)
    # Subject-level findings carry the subject id; the site-level finding does not.
    subject_visible_to: tuple[Role, ...] = (
        Role.CLINICAL_OPS_LEAD,
        Role.MEDICAL_MONITOR,
        Role.QA_AUDITOR,
    )
    site_visible_to: tuple[Role, ...] = (
        Role.CLINICAL_OPS_LEAD,
        Role.MEDICAL_MONITOR,
        Role.QA_AUDITOR,
        Role.PORTFOLIO_LEAD,
    )

    @model_validator(mode="after")
    def _windows(self) -> SaeReconcilerConfig:
        if self.fallback_window_days < self.onset_tolerance_days:
            raise ValueError("fallback_window_days must be at least onset_tolerance_days")
        if self.term_mismatch_days > self.fallback_window_days:
            raise ValueError("term_mismatch_days may not exceed fallback_window_days")
        if not set(self.subject_visible_to) <= set(self.site_visible_to):
            raise ValueError("subject-level findings may not reach more roles than site-level")
        return self


# DM fields that may give a site's first and last subject (SDTM DM reference dates).
SubjectStartField = Literal["RFICDTC", "RFSTDTC"]
SubjectEndField = Literal["RFENDTC", "RFPENDTC"]


class ToxStudyMonitorConfig(AgentConfig):
    """preclinical.tox_study_monitor 1.1.0 (1.0.0 in phase 5 step 1, 1.1.0 in step 1b).
    Every method and threshold is pre-registered in docs/preclinical-eval.md, set before
    the version ran on any study and not changed in response to a result. Comparisons are
    against the concurrent control only, never historical data. Visibility lives here,
    next to the agent."""

    # Continuous endpoints: Dunnett (many-to-one, two-sided) and Jonckheere-Terpstra
    # (ordered groups, two-sided). A dose group is flagged when Dunnett p < alpha_pairwise
    # and the change meets the magnitude, with and without its most extreme animal.
    alpha_pairwise: float = Field(0.05, gt=0, lt=1)
    alpha_trend: float = Field(0.01, gt=0, lt=1)
    # A flagged change with no trend is raised only when Dunnett p < this.
    alpha_pairwise_alone: float = Field(0.01, gt=0, lt=1)
    # Minimum absolute percent difference from the control mean, by endpoint class.
    magnitude_pct: dict[str, float] = Field(
        default_factory=lambda: {
            "body_weight": 5.0,
            "body_weight_gain": 10.0,
            "hematology": 10.0,
            "coagulation": 10.0,
            "clinical_chemistry": 20.0,
            "urinalysis": 25.0,
            "organ_weight": 10.0,
            "other": 20.0,
        }
    )
    # A group with fewer usable values is not tested; a control with fewer is reported.
    min_per_group: int = Field(3, ge=2)
    # Incidence endpoints: Cochran-Armitage (one-sided, increase; scores = dose rank) and
    # Fisher's exact test against control (one-sided, increase).
    alpha_incidence_trend: float = Field(0.05, gt=0, lt=1)
    alpha_incidence_pairwise_alone: float = Field(0.01, gt=0, lt=1)
    # The top dose group must exceed the control by at least this many animals and points.
    incidence_min_excess_animals: int = Field(2, ge=1)
    incidence_min_excess_points: float = Field(20.0, ge=0, le=100)
    # scipy.stats.dunnett integrates a multivariate t numerically; a fixed seed makes it
    # reproducible run to run (and so the drafts byte for byte).
    dunnett_seed: int = 20261115
    # 1.1.0: red cell mass parameters are flagged at this magnitude when another of them is
    # flagged at it in the same direction at the same dose (coherence); else the
    # hematology magnitude applies.
    red_cell_tests: tuple[str, ...] = ("RBC", "HGB", "HCT")
    red_cell_magnitude_pct: float = Field(3.0, gt=0)
    # 1.1.0: relative organ weights at a dose whose body weight is at least this far below
    # control, with no matching absolute change, become one "may be secondary" finding.
    bw_secondary_pct: float = Field(10.0, gt=0)
    # 1.1.0: recovery status; a change persists when the recovery difference is at least
    # this fraction of the main-study difference.
    recovery_persists_fraction: float = Field(0.5, gt=0, le=1)
    visible_to: tuple[Role, ...] = (Role.TOXICOLOGIST, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR)


class TmfCompletenessConfig(AgentConfig):
    """quality.tmf_completeness: the expected artifacts (config/tmf_expected.toml), the DM
    fields that give the site milestones, and who may see its findings. Visibility lives
    here, next to the agent, not in the API. No TMF finding names a subject."""

    reference: TmfReference = Field(default_factory=load_tmf_reference)
    # A site's first subject is the earliest of this field over its subjects, and its last
    # subject the latest of last_subject_field. RFICDTC (informed consent) would be the
    # better start, but it is empty for every subject of the CDISC pilot, so RFSTDTC (first
    # study treatment) is used; docs/tmf-notes.md says what that misses.
    first_subject_field: SubjectStartField = "RFSTDTC"
    last_subject_field: SubjectEndField = "RFENDTC"
    visible_to: tuple[Role, ...] = (
        Role.CLINICAL_OPS_LEAD,
        Role.MEDICAL_MONITOR,
        Role.PORTFOLIO_LEAD,
        Role.QA_AUDITOR,
    )


class AuditTrailReviewerConfig(AgentConfig):
    """quality.audit_trail_reviewer: thresholds of the audit checks, and who may see its
    findings. Defaults are documented in config/default.toml and docs/audit-notes.md."""

    # audit_rapid_decisions: at least this many decisions by one actor within this many
    # minutes (first to last, inclusive).
    rapid_decisions: int = Field(10, ge=2)
    rapid_window_minutes: float = Field(5.0, gt=0)
    # audit_reason_quality: a dismissal reason shorter than this many characters, one of
    # these placeholders (compared lower case, without punctuation), or identical to at
    # least reuse_others other dismissal reasons of the same actor.
    min_reason_chars: int = Field(12, ge=1)
    placeholder_reasons: tuple[str, ...] = (
        "ok",
        "okay",
        "n/a",
        "na",
        "none",
        "dismiss",
        "dismissed",
        "test",
        "testing",
        "fine",
        "done",
        "x",
        "-",
        "no issue",
        "not an issue",
        "false positive",
    )
    reuse_others: int = Field(3, ge=1)
    # audit_failed_run_unresolved: a run that started at most this many seconds before the
    # review may still be running ([runtime] exec_timeout_seconds), so it is not failed yet.
    in_progress_seconds: int = Field(120, ge=0)
    # Audit findings name people and runs, never subjects; segregation of duties keeps them
    # to QA and clinical operations (clinical_ops_lead decides; qa_auditor never decides).
    visible_to: tuple[Role, ...] = (Role.CLINICAL_OPS_LEAD, Role.QA_AUDITOR)


# Phase 5 step 2. The FDA 2005 guidance's figures, each with its citation; the agent uses
# no factor that is not here (docs/fih-dose-notes.md).
FDA_2005 = (
    "FDA, Guidance for Industry: Estimating the Maximum Safe Starting Dose in Initial "
    "Clinical Trials for Therapeutics in Adult Healthy Volunteers (July 2005)"
)


class SpeciesConversion(AgentConfig):
    """One row of the guidance's Table 1 (section V.A): to convert an animal dose in
    mg/kg to the human equivalent dose in mg/kg, divide by divide_by (or multiply by
    multiply_by); km converts mg/kg to mg/m2."""

    km: float = Field(gt=0)
    divide_by: float = Field(gt=0)
    multiply_by: float = Field(gt=0)
    citation: str = Field(min_length=1)


class DoseTranslationConfig(AgentConfig):
    """preclinical.dose_translation 1.0.0: NOAEL to HED to MRSD by the FDA 2005 guidance."""

    guidance: str = FDA_2005
    # Table 1, by SEND TS SPECIES. A species not listed gets no starting dose.
    conversions: dict[str, SpeciesConversion] = Field(default_factory=dict)
    human_km: float = Field(37.0, gt=0)
    human_km_citation: str = "section V.A, Table 1: Human, km 37"
    # Section VII: "The default safety factor that should normally be used is 10."
    safety_factor: float = Field(10.0, ge=1)
    safety_factor_citation: str = (
        'section VII: "The default safety factor that should normally be used is 10."'
    )
    # Table 1, footnote a: "Assumes 60 kg human."
    human_weight_kg: float = Field(60.0, gt=0)
    human_weight_citation: str = 'section V.A, Table 1, footnote a: "Assumes 60 kg human."'
    visible_to: tuple[Role, ...] = (
        Role.TOXICOLOGIST,
        Role.MEDICAL_MONITOR,
        Role.PORTFOLIO_LEAD,
        Role.QA_AUDITOR,
    )


class IndRequirement(AgentConfig):
    """One requirement of config/ind_requirements.toml, with its guidance and section."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    study_types: tuple[str, ...] = Field(min_length=1)
    species_class: str
    duration: Literal["table1", "none"]
    applies: Literal["always", "multiple_dose"]
    source: str = Field(min_length=1)
    section: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    note: str = ""


class IndRequirements(AgentConfig):
    classes: dict[str, tuple[str, ...]]
    requirements: tuple[IndRequirement, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _known_classes(self) -> IndRequirements:
        for r in self.requirements:
            if r.species_class != "any" and r.species_class not in self.classes:
                raise ValueError(f"{r.id}: unknown species class {r.species_class!r}")
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("a requirement id is used twice")
        return self


IND_REQUIREMENTS_FILE = "ind_requirements.toml"


def load_ind_requirements(path: Path | None = None) -> IndRequirements:
    """config/ind_requirements.toml next to the config file in use."""
    config = os.environ.get(CONFIG_FILE_ENV)
    default = (Path(config).parent if config else Path("config")) / IND_REQUIREMENTS_FILE
    with (path or default).open("rb") as handle:
        return IndRequirements.model_validate(tomllib.load(handle))


class IndReadinessConfig(AgentConfig):
    """preclinical.ind_readiness 1.0.0 (docs/ind-readiness-eval.md, pre-registered)."""

    asset_id: str = "MRD-101"
    # MRD-101's first-in-human trial, assumed single and multiple ascending doses of at most
    # 14 days (no design is recorded anywhere; MRD-110 is a separate asset).
    planned_clinical_duration_days: int = Field(14, ge=1)
    multiple_dose: bool = True
    planned_submission_date: date = date(2027, 4, 1)
    requirements: IndRequirements = Field(default_factory=load_ind_requirements)
    visible_to: tuple[Role, ...] = (Role.TOXICOLOGIST, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR)


class MonitoringSource(AgentConfig):
    source: str = Field(min_length=1)
    section: str = Field(min_length=1)
    quote: str = Field(min_length=10)


class OrganMonitoring(AgentConfig):
    """One entry of config/translational_monitoring.toml."""

    id: str = Field(pattern=r"^[a-z_]+$")
    label: str = Field(min_length=1)
    matches: tuple[str, ...] = Field(min_length=1)
    measure: str = Field(min_length=1)
    frequency: str = Field(min_length=1)
    review: str = Field(min_length=1)
    stopping: str = Field(min_length=1)
    sources: tuple[MonitoringSource, ...] = Field(min_length=1)


class TranslationalMapping(AgentConfig):
    basis: MonitoringSource
    organs: tuple[OrganMonitoring, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> TranslationalMapping:
        if len({o.id for o in self.organs}) != len(self.organs):
            raise ValueError("an organ id is used twice")
        return self


TRANSLATIONAL_FILE = "translational_monitoring.toml"


def load_translational_mapping(path: Path | None = None) -> TranslationalMapping:
    """config/translational_monitoring.toml next to the config file in use."""
    config = os.environ.get(CONFIG_FILE_ENV)
    default = (Path(config).parent if config else Path("config")) / TRANSLATIONAL_FILE
    with (path or default).open("rb") as handle:
        return TranslationalMapping.model_validate(tomllib.load(handle))


class TranslationalBridgeConfig(AgentConfig):
    """preclinical.translational_bridge 1.0.0 (docs/translational-notes.md, pre-registered).
    dose is [agents.dose_translation] itself (AgentsSettings copies it in), so the margins
    use the same conversion factor and citation as the dose proposal."""

    asset_id: str = "MRD-101"
    mapping: TranslationalMapping = Field(default_factory=load_translational_mapping)
    dose: DoseTranslationConfig = Field(default_factory=DoseTranslationConfig)
    visible_to: tuple[Role, ...] = (
        Role.TOXICOLOGIST,
        Role.MEDICAL_MONITOR,
        Role.PORTFOLIO_LEAD,
        Role.QA_AUDITOR,
    )


FAERS_CAVEATS: tuple[str, ...] = (
    "There is no certainty that the reported event (adverse event or medication error) "
    "was actually due to the product.",
    "FDA does not require that a causal relationship between a product and event be proven",
    "FDA does not receive reports for every adverse event or medication error that occurs "
    "with a product.",
    "Many factors can influence whether or not an event will be reported",
    "The information in these reports has not been scientifically or otherwise verified as "
    "to a cause and effect relationship and cannot be used to estimate the incidence of "
    "these events.",
)


class SignalScoutConfig(AgentConfig):
    """portfolio.safety_signal_scout 1.0.0 and 1.0.1 (docs/signal-scout.md, each
    pre-registered). 1.0.1: retry openFDA's throttling, context on every signal, triage."""

    study_id: str = "MRD-201"
    drug: str = "xanomeline (with trospium chloride, COBENFY)"
    base_url: str = "https://api.fda.gov"
    product_field: str = "patient.drug.openfda.generic_name.exact"
    product_term: str = "XANOMELINE AND TROSPIUM CHLORIDE"
    total_field: str = "fulfillexpeditecriteria"
    limit: int = Field(999, ge=1, le=999)  # 1000 needs an API key
    min_reports: int = Field(3, ge=1)
    prr_min: float = Field(2.0, gt=0)
    chi2_min: float = Field(4.0, ge=0)
    request_interval_seconds: float = Field(0.3, ge=0)
    max_requests: int = Field(900, ge=5, le=1000)  # openFDA: 1,000 a day per IP, no key
    request_timeout_seconds: float = Field(30.0, gt=0)
    caveats_source: str = "open.fda.gov/apis/drug/event (Disclaimer)"
    caveats: tuple[str, ...] = FAERS_CAVEATS
    visible_to: tuple[Role, ...] = (Role.MEDICAL_MONITOR, Role.PORTFOLIO_LEAD)
    # 1.0.1 (phase 5 step 3b).
    retry_backoff_seconds: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)
    priority_prr_low_above: float = Field(2.0, gt=0)
    priority_min_reports: int = Field(10, ge=1)
    indication_terms: tuple[str, ...] = ()
    indication_source: str = ""
    use_terms: tuple[str, ...] = ()
    label_spellings: tuple[tuple[str, str], ...] = ()
    label_synonyms: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    context: tuple[str, ...] = ()
    indication_caveat: str = ""


class PipelineIntelligenceConfig(AgentConfig):
    """portfolio.pipeline_intelligence 1.1.0 (docs/stage-gate-notes.md)."""

    visible_to: tuple[Role, ...] = (Role.PORTFOLIO_LEAD, Role.QA_AUDITOR)


class StageGatePackConfig(AgentConfig):
    """portfolio.stage_gate_pack 1.0.0: the gates of config/gates.toml (pre-registered)."""

    gates: Gates = Field(default_factory=load_gates)
    visible_to: tuple[Role, ...] = (Role.PORTFOLIO_LEAD, Role.QA_AUDITOR)

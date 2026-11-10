"""Configuration models of the agents. Kept outside meridian.agents so that settings and
the eval harness can read thresholds without importing any agent code."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.domain.enums import Role
from meridian.platform.tmf_reference import TmfReference, load_tmf_reference


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

"""Closed vocabularies of the domain."""

from enum import StrEnum


class Role(StrEnum):
    PORTFOLIO_LEAD = "portfolio_lead"
    TOXICOLOGIST = "toxicologist"
    CLINICAL_OPS_LEAD = "clinical_ops_lead"
    MEDICAL_MONITOR = "medical_monitor"
    QA_AUDITOR = "qa_auditor"
    UNBLINDED_STATISTICIAN = "unblinded_statistician"


class Visibility(StrEnum):
    BLINDED = "blinded"
    RESTRICTED = "restricted"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FindingKind(StrEnum):
    KRI_BREACH = "kri_breach"
    QTL_BREACH = "qtl_breach"
    SITE_DATA_ANOMALY = "site_data_anomaly"
    UNDER_REPORTING = "under_reporting"
    SAE_RECONCILIATION = "sae_reconciliation"
    TMF = "tmf"
    AUDIT = "audit"
    QA_OBSERVATION = "qa_observation"


class EventType(StrEnum):
    DATA_CUT = "data_cut"
    DOCUMENT_FILED = "document_filed"
    ACTOR_WOKE = "actor_woke"
    ACTOR_SUSPENDED = "actor_suspended"
    FINDING_RAISED = "finding_raised"
    POLICY_DENIED = "policy_denied"
    CONTAINMENT_CHECK_COMPLETED = "containment_check_completed"
    FINDING_NARRATIVE = "finding_narrative"
    FINDING_ASSESSMENT = "finding_assessment"
    HUMAN_DECISION = "human_decision"
    AUDIT_REVIEW_REQUESTED = "audit_review_requested"
    QUALITY_OBSERVATION = "quality_observation"


class DecisionAction(StrEnum):
    ACCEPTED = "accepted"
    ESCALATED = "escalated"
    DISMISSED = "dismissed"

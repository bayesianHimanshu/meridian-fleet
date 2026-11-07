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


class EventType(StrEnum):
    DATA_CUT = "data_cut"
    DOCUMENT_FILED = "document_filed"
    ACTOR_WOKE = "actor_woke"
    ACTOR_SUSPENDED = "actor_suspended"
    FINDING_RAISED = "finding_raised"
    POLICY_DENIED = "policy_denied"
    HUMAN_DECISION = "human_decision"


class DecisionAction(StrEnum):
    ACCEPTED = "accepted"
    ESCALATED = "escalated"
    DISMISSED = "dismissed"

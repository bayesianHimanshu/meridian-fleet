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
    TOX_SIGNAL = "tox_signal"  # preclinical: a dose-related or pairwise change, or data quality
    # Phase 5 step 2: a proposed starting dose for the first human trial (or why none can
    # be derived), and a gap between the nonclinical inventory and the FIH requirements.
    DOSE_PROPOSAL = "dose_proposal"
    IND_READINESS = "ind_readiness"
    # Phase 5 step 3a: how a first-in-human study should monitor the NOAEL's target organs.
    MONITORING_RECOMMENDATION = "monitoring_recommendation"
    # Phase 5 step 3a: a signal of disproportionate reporting in public FAERS data.
    REPORTING_SIGNAL = "reporting_signal"
    # Phase 5 step 3b: report kinds. An agent returns them as drafts; the runner records
    # a PortfolioOutlook or GatePack event, never a finding.
    PORTFOLIO_OUTLOOK = "portfolio_outlook"
    GATE_PACK = "gate_pack"


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
    SEND_STUDY_LOADED = "send_study_loaded"
    NOAEL_DECISION = "noael_decision"
    INVENTORY_LOADED = "inventory_loaded"
    SIGNAL_SCAN_REQUESTED = "signal_scan_requested"
    # Phase 5 step 3b.
    ATTRIBUTION_CORRECTION = "attribution_correction"
    PORTFOLIO_REVIEW_REQUESTED = "portfolio_review_requested"
    PORTFOLIO_OUTLOOK = "portfolio_outlook"
    GATE_REQUEST = "gate_request"
    GATE_PACK = "gate_pack"
    GATE_DECISION = "gate_decision"


class DecisionAction(StrEnum):
    ACCEPTED = "accepted"
    ESCALATED = "escalated"
    DISMISSED = "dismissed"

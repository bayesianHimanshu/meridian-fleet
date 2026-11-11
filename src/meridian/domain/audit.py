"""The audit extract: what quality.audit_trail_reviewer may see of the ledger, and its checks.

The host exports one row per ledger event up to the reviewed seq (meridian.audit.extract)
and stages it as /stage/audit/audit_extract.parquet, readable only under
policies/quality.audit_trail.yaml. Shared by the exporter and the agent; nothing here reads
data.

A row holds the envelope (seq, hashes, type, time, actor) and, per event type, the
identifiers a review needs: the finding id, the cut id and its scope, the agent, version,
executor, image digest and policy hashes of a run (for a Substrate run, the template's
spec hash and the golden snapshot id), and a decision's role, action and
reason. Never evidence, narratives, subject ids, titles or summaries. A restricted event
(or one about a restricted finding) keeps only seq, hashes, type and time, with REDACTED
= "Y".

FINDING_GROUP stands for a finding's identity without its version: every version's finding
with the same agent, cut, kind, site and check gets the same small integer, numbered in
order of first appearance. The reviewer can tell that a newer version raised the same thing
(supersession) without seeing the check, which may name a subject.
"""

SEQ = "SEQ"
HASH = "HASH"
PREV_HASH = "PREV_HASH"
TYPE = "TYPE"  # the event type (meridian.domain.enums.EventType value)
RECORDED_AT = "RECORDED_AT"  # ISO 8601 UTC
REDACTED = "REDACTED"  # "Y" for a row that keeps only seq, hashes, type and time
ACTOR = "ACTOR"  # the event's actor: an agent id, a person id or a host component
ROLE = "ROLE"  # HumanDecision: the deciding role
FINDING_ID = "FINDING_ID"  # FindingRaised, HumanDecision, FindingNarrative, ...
FINDING_GROUP = "FINDING_GROUP"  # FindingRaised: identity without the version (see above)
CUT_ID = "CUT_ID"  # DataCut; FindingRaised; ActorWoke and ActorSuspended (their trigger's)
CUT_SCOPE = "CUT_SCOPE"  # operational or evaluation
AGENT = "AGENT"
VERSION = "VERSION"
TRIGGER_SEQ = "TRIGGER_SEQ"  # ActorWoke, ActorSuspended
EXECUTOR = "EXECUTOR"
IMAGE_DIGEST = "IMAGE_DIGEST"
POLICY_SHA256 = "POLICY_SHA256"  # the applied policy
POLICY_FILE_SHA256 = "POLICY_FILE_SHA256"
RUN_STATUS = "RUN_STATUS"  # ActorWoke: started; ActorSuspended: completed
# ActorWoke: when the run started, ISO 8601 UTC (phase 4a step 3: ActorWoke is appended
# after the run, so RECORDED_AT is its commit). Empty on older events: use RECORDED_AT.
STARTED_AT = "STARTED_AT"
DECISION_ACTION = "DECISION_ACTION"
DECISION_REASON = "DECISION_REASON"
# Phase 4a: a run in Agent Substrate (ActorSuspended with executor substrate) records the
# template's spec hash and the golden snapshot the actor was cloned from.
TEMPLATE_SHA256 = "TEMPLATE_SHA256"
GOLDEN_SNAPSHOT = "GOLDEN_SNAPSHOT"

COLUMNS: tuple[str, ...] = (
    SEQ,
    HASH,
    PREV_HASH,
    TYPE,
    RECORDED_AT,
    REDACTED,
    ACTOR,
    ROLE,
    FINDING_ID,
    FINDING_GROUP,
    CUT_ID,
    CUT_SCOPE,
    AGENT,
    VERSION,
    TRIGGER_SEQ,
    EXECUTOR,
    IMAGE_DIGEST,
    POLICY_SHA256,
    POLICY_FILE_SHA256,
    RUN_STATUS,
    DECISION_ACTION,
    DECISION_REASON,
    TEMPLATE_SHA256,
    GOLDEN_SNAPSHOT,
    STARTED_AT,
)
INT_COLUMNS: tuple[str, ...] = (SEQ, FINDING_GROUP, TRIGGER_SEQ)
# The only columns a redacted row fills.
REDACTED_COLUMNS: tuple[str, ...] = (SEQ, HASH, PREV_HASH, TYPE, RECORDED_AT, REDACTED)

AUDIT_EXTRACT = "audit_extract"  # the table name (audit_extract.parquet)
STARTED = "started"
COMPLETED = "completed"
OPERATIONAL = "operational"
EVALUATION = "evaluation"
# The cut id every audit finding carries in its dedup key: a review covers the ledger, not
# a data cut, so the same occurrence found by two reviews has one key and is raised once.
LEDGER_SCOPE = "LEDGER"

# The checks of quality.audit_trail_reviewer.
CHAIN_LINKAGE = "audit_chain_linkage"
UNSANDBOXED_OPERATIONAL_RUN = "audit_unsandboxed_operational_run"
FAILED_RUN_UNRESOLVED = "audit_failed_run_unresolved"
RAPID_DECISIONS = "audit_rapid_decisions"
REASON_QUALITY = "audit_reason_quality"
DECISION_ON_RETIRED = "audit_decision_on_retired"
DATA_QUALITY = "audit_data_quality"
CHECKS: tuple[str, ...] = (
    CHAIN_LINKAGE,
    UNSANDBOXED_OPERATIONAL_RUN,
    FAILED_RUN_UNRESOLVED,
    RAPID_DECISIONS,
    REASON_QUALITY,
    DECISION_ON_RETIRED,
    DATA_QUALITY,
)


def check_id(check: str, record: str) -> str:
    return f"{check}:{record}"

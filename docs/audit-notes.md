# Audit trail review and QA observations (phase 3 step 4b)

## What the reviewer may see

`meridian audit review` appends an AuditReviewRequested event (seq 1 through the event
before it), exports the audit extract to var/audit/<trigger seq>/audit/audit_extract.parquet
and runs quality.audit_trail_reviewer in an OpenShell sandbox under
policies/quality.audit_trail.yaml: it reads /stage/meta and /stage/audit only, has no
network, and is proved against its own boundary (policies/boundaries/audit.yaml). No other
policy lists /stage/audit; conformance shows every other policy refused there. The
ActorWoke of the run records the reviewed range (review_from_seq, review_to_seq). The
review refuses to run outside the sandbox, as an operational cut would.

One row per event (src/meridian/domain/audit.py): seq, hash, prev_hash, type, recorded_at,
actor; the role, action and reason of a decision; the finding id; the cut and its scope;
agent, version, trigger seq, executor, image digest and policy hashes of a run. Never
evidence, titles, summaries, narratives, assessment or observation text. A finding's check
can name a subject, so it is replaced by FINDING_GROUP, a small number shared by every
version's finding with the same agent, cut, kind, site and check. Subject ids typed into a
decision reason are masked as "[subject]". A restricted event, or one about a restricted
finding, keeps only seq, hashes, type and time.

## The checks and their defaults ([agents.audit_trail_reviewer])

| Check | Severity | Rule |
|---|---|---|
| audit_chain_linkage | high | every prev_hash is the previous row's hash (genesis for seq 1), no seq gap, the extract reaches the reviewed seq. Linkage only: hashes are not recomputed, because restricted payloads are not exported; `meridian ledger verify` stays the full check |
| audit_unsandboxed_operational_run | high | a run on an operational cut not started and completed in openshell, or completed without an image digest or applied policy hash |
| audit_failed_run_unresolved | medium | a run on an operational cut that never completed, with no later completed run of the same agent on that cut; a run started under 120 s before the review may still be running |
| audit_rapid_decisions | medium | 10 or more decisions by one actor within 5 minutes, first to last inclusive: a signal of review without reading, not proof |
| audit_reason_quality | low | a dismissal reason under 12 characters or a placeholder ("ok", "n/a", "test", ...); and, once per actor and reason, one identical to 3 or more other dismissals by that actor |
| audit_decision_on_retired | medium | a decision recorded after its finding was superseded (a newer version raised the same group) or retired (a newer version completed a run on the cut without it) |
| audit_data_quality | medium | a malformed row; the checks that need the field skip it |

## Segregation of duties

- Audit findings: visible to qa_auditor and clinical_ops_lead; decided by
  clinical_ops_lead only. A finding about a person's decisions names them
  (actor_under_review).
- QA observations: raised by qa_auditor only (POST /quality/observations), visible to
  qa_auditor, clinical_ops_lead and medical_monitor (fewer when the finding or event it
  refers to reaches fewer), decided by clinical_ops_lead or medical_monitor. The finding's
  agent id is "human.observation" and raised_by names the auditor; an observation about a
  decision names its decider as the actor under review.
- For every finding kind, a person may not decide a finding that names them as the actor
  under review, or one they raised (403). qa_auditor never decides anything.

## Known failure modes

- Rapid decisions are counted by recorded time; a person deciding quickly after reading
  carefully offline is flagged, and one reading nothing at a slower pace is not.
- Reason reuse compares normalised text: one changed word hides a copied reason.
- Every phase 1 run predates sandboxes and is flagged as unsandboxed on DC-BASE: true, but
  historical. There is no rule date in the check.
- A run whose text has an empty version reads "rbqm.csm  on DC-BASE" (two spaces): a
  cosmetic flaw left in 1.0.0 because the agent was frozen after the stress run.

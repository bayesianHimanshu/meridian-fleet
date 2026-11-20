# quality.audit_trail_reviewer: evaluation

## Targets (stated before running)

1. Stress set (tests/fixtures/audit_stress, 16 hand-written extracts, committed in d16cfd8
   with a timestamp fix in 5a27fe6, before any agent code): run once at the end; report
   each case's ideal against actual; do not change the agent afterwards. No numeric target.
2. Real ledger, in a sandbox: report every finding, and check them against the known
   facts: the two decisions at seq 787 and 788, the 11 safety runs that failed on 8 Nov
   and were later resolved, and the in-process runs, all on evaluation cuts.

## Stress set (run once, 10 Nov 2026)

| # | Case | Ideal | Actual |
|---|---|---|---|
| 01 | seq gap | chain gap-3-4 | same |
| 02 | broken prev_hash | chain link-3 | same |
| 03 | in-process run on an evaluation cut | nothing | nothing |
| 04 | unsandboxed runs on an operational cut | 2 unsandboxed (csm in process, qtl without image) | same |
| 05 | failed run later fixed | nothing | nothing |
| 06 | failed run never fixed | failed safety.sae_reconciler/1.0.0/1/2 | same |
| 07 | 9 decisions in 5 minutes | nothing | nothing |
| 08 | 10 decisions in exactly 5 minutes | rapid asha.rao/6 | same |
| 09 | 10 decisions over an hour | nothing | nothing |
| 10 | one detailed reason on 5 dismissals | reason reused asha.rao/6 | same |
| 11 | "ok" as a reason | reason quality 5 | same |
| 12 | decision on a retired finding | decision on retired 7 | same |
| 13 | decision on a superseded finding | decision on retired 9 | same |
| 14 | clean: small operational ledger with a redacted row | nothing | nothing |
| 15 | clean: mixed executors | nothing | nothing |
| 16 | clean: two people, distinct reasons | nothing | nothing |

16 of 16 match. Said plainly, as in step 4a: every ideal above was written by the same
model that then wrote the agent, from the same specification, and the agent was written
knowing them. This tests that the agent implements the specification. It says nothing
about cases nobody wrote down (see the failure modes in docs/audit-notes.md).

## The real ledger (sandboxed, 10 Nov 2026)

`meridian audit review`: AuditReviewRequested at seq 4549, reviewing seq 1 to 4548;
ActorWoke seq 4550 (openshell), ActorSuspended seq 4558 (image sha256:565b323a...,
policies/quality.audit_trail.yaml). Chain linkage intact. 7 findings, QA-0150 to QA-0156,
all audit_unsandboxed_operational_run (high):

| Finding | Run | Ledger |
|---|---|---|
| QA-0150 | rbqm.csm (no version) on DC-BASE | completed seq 17 |
| QA-0151 | rbqm.csm 2.0.0 on DC-BASE | completed seq 59 |
| QA-0152 | rbqm.kri_monitor (no version) on DC-BASE | completed seq 37 |
| QA-0153 | rbqm.kri_monitor 2.0.0 on DC-BASE | completed seq 75 |
| QA-0154 | rbqm.kri_monitor 2.1.0 on DC-BASE | completed seq 290 |
| QA-0155 | rbqm.qtl_detector (no version) on DC-BASE | completed seq 49 |
| QA-0156 | rbqm.qtl_detector 2.0.0 on DC-BASE | completed seq 99 |

All seven are phase 1 runs of 7 Nov (seq 3 to 290), before agents ran in sandboxes: the
executor was not recorded and there is no image or policy hash. They are true by the rule
and historical; clinical operations can close them with that reason.

Against the known facts:

- Seq 787 and 788: asha.rao accepted RBQM-0136 and dismissed RBQM-0134, 47 ms apart, with
  long and distinct reasons, on findings current at the time. Not flagged: 2 decisions are
  far below 10 in 5 minutes. (Two decisions 47 ms apart were scripted, not read; the
  threshold is not built to see that.)
- The 11 safety.sae_reconciler runs that failed on 8 Nov (one on DC-BASE, ten on
  evaluation cuts): not flagged. The DC-BASE one was resolved by a later completed run of
  the same agent on the cut, and evaluation cuts are out of scope.
- In-process runs: 1,264 run events with executor inprocess, all on evaluation cuts: none
  flagged.

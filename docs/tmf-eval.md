# quality.tmf_completeness: evaluation

Step 4a. Targets were written and committed before any run; results follow below them.

## What this evaluation can and cannot show

The generated sets are a regression check by construction, as step 3a found for SAE
reconciliation. The eTMF generator, the fault injector and the agent read the same
reference list (config/tmf_expected.toml), the same milestone derivation from DM
(meridian.tmf.milestones) and the same column names (meridian.domain.tmf). A perfect
score shows that the agent finds what the injector plants under rules both share; it
says nothing about whether those rules are the right ones, or about records shaped in ways
the generator never makes. That is what the hand-written stress set is for
(tests/fixtures/tmf_stress/, committed before any agent code): its ideal outcomes were
written by hand, without the generator or the agent's normalisation, and it was run once,
at the end, with the agent frozen afterwards.

## Sets

| Set | Cuts | Faults per cut | Seed |
|---|---|---|---|
| dev | 30 | 1 to 3 | 20261120 |
| holdout | 30 | 1 to 3 | 20261121 |
| clean | 10 | none | 20261122 |
| DC-BASE | 1 | none (operational) | eTMF seed from "etmf\|DC-BASE" |

Cut ids are opaque ("DC-" and 8 hex of sha256 of the scenario's canonical JSON); the
readable label (tmf_dev_01 ...) is only in the ground truth. Each cut's clinical data is
DC-BASE's; only its eTMF snapshot differs.

## Targets (set before running)

1. dev and holdout: precision 1.00 and recall 1.00 for every check that has planted
   records (tmf_missing_artifact, tmf_late_filing, tmf_approval_after_first_subject,
   tmf_misfiled, tmf_monitoring_report_missing, tmf_qc_overdue). Anything less is a bug
   in the agent or the injector, not a measured accuracy.
2. clean cuts and DC-BASE: 0 findings, and 0 of the harmless variations (several
   versions, a superseded version filed late, name case and whitespace, same-day filing,
   a QC rejection with a passed resubmission, a report dated 1 to 3 days after its visit)
   flagged.
3. tmf_data_quality: 0 findings on every generated cut (the generator writes no bad
   record; the stress set and the unit tests exercise that path).
4. Every run in an OpenShell sandbox under policies/quality.yaml, 0 failed runs, on
   DC-BASE and all 70 generated cuts.
5. Stress set: no numeric target. Each case is reported ideal against actual once, and
   the agent is not changed after that run.

## Results

Run on 10 Nov 2026 with quality.tmf_completeness 1.0.0, image sha256:aa300ee9...c5e097,
policies/quality.yaml. Every run was in an OpenShell sandbox: DC-BASE and all 70
generated cuts, 71 runs, 0 failed (target 4 met). Scores from `meridian tmf eval`.

| Check | dev P | dev R | dev TP/FP/FN | holdout P | holdout R | holdout TP/FP/FN |
|---|---|---|---|---|---|---|
| tmf_missing_artifact | 1.00 | 1.00 | 20/0/0 | 1.00 | 1.00 | 22/0/0 |
| tmf_late_filing | 1.00 | 1.00 | 10/0/0 | 1.00 | 1.00 | 12/0/0 |
| tmf_approval_after_first_subject | 1.00 | 1.00 | 10/0/0 | 1.00 | 1.00 | 11/0/0 |
| tmf_misfiled | 1.00 | 1.00 | 10/0/0 | 1.00 | 1.00 | 11/0/0 |
| tmf_monitoring_report_missing | 1.00 | 1.00 | 10/0/0 | 1.00 | 1.00 | 12/0/0 |
| tmf_qc_overdue | 1.00 | 1.00 | 10/0/0 | 1.00 | 1.00 | 11/0/0 |
| tmf_data_quality | n/a | n/a | 0/0/0 | n/a | n/a | 0/0/0 |

tmf_missing_artifact counts include the missing record each misfile leaves at its own
site (10 in dev, 11 in holdout). Findings: dev 70, holdout 79.

| Set | Cuts | Findings | Harmless variations present | Flagged |
|---|---|---|---|---|
| clean | 10 | 0 | 1214 | 0 |
| DC-BASE | 1 | 0 | 127 | 0 |
| dev | 30 | 70 (all planted) | 3546 | 0 |
| holdout | 30 | 79 (all planted) | 3719 | 0 |

Targets 1 to 3 met. As said above, this is the regression check, not evidence of
accuracy on real TMF data. DC-BASE stays operational: 100% complete and 100% filed on
time, 0 findings, so nothing reaches the review queue from it.

The 111 earlier evaluation cuts (RBQM and safety) also got a clean eTMF snapshot, because
the agent wakes on every DataCut; it ran on those in process (evaluation cuts may), with
0 findings. In the ledger: 182 runs of the agent, all completed; 71 openshell, 111
inprocess; all 149 of its findings are on the TMF sets.

## Stress set (run once, at the end)

The 15 cases of tests/fixtures/tmf_stress (committed in 125bea3, before any agent code),
run once with `PYTHONPATH=. uv run python scripts/tmf_stress.py`. The agent was not
changed afterwards.

| # | Case | Ideal | Actual |
|---|---|---|---|
| 01 | amendment approved after first subject, original on time | nothing | nothing |
| 02 | two versions, only the superseded one late | nothing | nothing |
| 03 | document dated in the future | data quality D-T-MP2#DOCUMENT_DATE | same |
| 04 | site activated, never enrolled | nothing | nothing |
| 05 | site closed early | nothing | nothing |
| 06 | misfiled copy, right site has its own | misfiled D-1-1572X | same |
| 07 | report filed under the next visit's date | report missing V-9001-R2 | same |
| 08 | QC rejected, passed resubmission | nothing | nothing |
| 09 | approval after first subject | approval 9002/irb_iec_decision | same |
| 10 | unreadable filed date | data quality D-1-DEL#FILED_DATE | same |
| 11 | QC overdue and late close-out | qc overdue D-1-MV5, late D-1-CO | same |
| 12 | missing trial CRF, site 1572 and a visit report | the three records | same |
| 13 | clean: base | nothing | nothing |
| 14 | clean: harmless variations | nothing | nothing |
| 15 | clean: at every limit | nothing | nothing |

15 of 15 match. Read this with care: the ideals were written first, but the agent's rules
were then written to them (a bad filed date still counts as filed because case 10 says a
reviewer would not call the record missing; the visit-matching rule follows case 07). So
the set shows the agent implements what the cases specify. It is not a blind test: the
cases a reviewer did not think of are exactly what it cannot cover. The known failure
modes in docs/tmf-notes.md are the cases to add next.

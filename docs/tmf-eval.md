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

# safety.sae_reconciler 1.0.0: evaluation

Targets and the evaluation design were written and committed before the first run on the
evaluation set. Results are added below them as they come out, without tuning anything on
holdout.

## Design

- **Ground truth per record.** Every planted fault records its check and the record it
  touches: the clinical record key (subject/AESEQ) for a missing case and the onset,
  outcome and causality mismatches, the case id for a case without a clinical SAE and a
  late report, the site for late reporting at site level. A finding is a true positive
  when its check and record key match a planted record of its cut.
- **Sets** (fixed, seeded, `meridian.faults.safety.scenario_set`):
  - dev: 30 cuts, 1 to 3 faults each, fault types spread evenly;
  - holdout: 30 cuts, the same design with another seed, scored once at the end;
  - clean: 10 cuts, the same augmentation and harmless differences, no fault;
  - DC-BASE: the study's current cut with its clean safety database.
- **Augmentation.** DC-BASE has 3 serious AEs, too few for record-level scoring. Each
  evaluation cut promotes 6 to 12 seeded non-serious AEs with a full onset date to serious
  before its safety database is generated. A (subject, preferred term) already serious, or
  occurring twice among the candidates, is never promoted.
- **Harmless differences**, drawn per case with probability 0.35 each: the preferred term
  in another case with extra whitespace, a reporter's verbatim term that differs from the
  clinical term, and an onset given as a date-time on the same day.
- **Runs.** The agent runs in OpenShell sandboxes on DC-BASE and the dev set, so the
  ledger holds real runs; the clean and holdout sets are run the same way, holdout last.
- **Scoring.** `meridian safety eval --set dev|holdout|clean|base` reads the ledger and
  writes `data/evals/safety_<set>.json`.

## Targets

| Measure | Target |
|---|---|
| Recall per check, dev | 1.0 |
| Precision per check, dev | 1.0 |
| Recall per check, holdout | 1.0 |
| Precision per check, holdout | 1.0 |
| Findings on the 10 clean cuts | 0 |
| Findings on DC-BASE | 0 |
| Each harmless difference type, across dev, holdout and clean | present at least 20 times |
| Harmless differences flagged | 0 |

The checks: sae_missing_in_safety_db, case_without_clinical_sae, onset_mismatch,
outcome_mismatch, causality_mismatch, late_sae_report, sae_late_reporting_site.

Configuration under evaluation (config/default.toml, `[agents.sae_reconciler]`): onset
tolerance 1 day, fallback window 3 days, late after 24 hours, a site flagged at 2 late
cases. Not changed in response to any result below.

## Results

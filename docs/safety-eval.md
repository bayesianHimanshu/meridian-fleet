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

Run on 2026-11-08 with the agent image built from this branch: DC-BASE and every set in
OpenShell sandboxes under policies/safety.sae_reconciler.yaml (71 runs, none failed),
holdout last and scored once. Scores from `meridian safety eval`.

| Check | dev P | dev R | dev TP/FP/FN | holdout P | holdout R | holdout TP/FP/FN |
|---|---|---|---|---|---|---|
| sae_missing_in_safety_db | 1.00 | 1.00 | 8/0/0 | 1.00 | 1.00 | 8/0/0 |
| case_without_clinical_sae | 1.00 | 1.00 | 8/0/0 | 1.00 | 1.00 | 8/0/0 |
| onset_mismatch | 1.00 | 1.00 | 8/0/0 | 1.00 | 1.00 | 9/0/0 |
| outcome_mismatch | 1.00 | 1.00 | 9/0/0 | 1.00 | 1.00 | 8/0/0 |
| causality_mismatch | 1.00 | 1.00 | 8/0/0 | 1.00 | 1.00 | 8/0/0 |
| late_sae_report | 1.00 | 1.00 | 28/0/0 | 1.00 | 1.00 | 32/0/0 |
| sae_late_reporting_site | 1.00 | 1.00 | 8/0/0 | 1.00 | 1.00 | 8/0/0 |

late_sae_report counts both the single late faults and each late case of a site-level
fault (each site fault plants 2 or more late cases), hence the larger numbers.

| Set | Cuts | Findings | Target | Met |
|---|---|---|---|---|
| dev | 30 | 77 (all true) | precision and recall 1.0 per check | yes |
| holdout | 30 | 81 (all true) | precision and recall 1.0 per check | yes |
| clean | 10 | 0 | 0 | yes |
| DC-BASE | 1 | 0 | 0 | yes |

| Harmless difference | dev | holdout | clean | Total | Flagged |
|---|---|---|---|---|---|
| term_case_whitespace | 118 | 132 | 50 | 300 | 0 |
| verbatim_term | 119 | 134 | 38 | 291 | 0 |
| datetime_onset | 131 | 126 | 37 | 294 | 0 |

DC-BASE's own clean safety database has one term variant and one date-time onset (3
cases), neither flagged.

### What these numbers do and do not show

Every target is met, and that was close to certain by construction. The generator and the
agent share one definition of "the same term" and "the same day"
(`meridian.domain.safety`), and each fault was designed with the matching rule in mind:
onset shifts are 2 or 3 days, inside the fallback window, and late single faults are kept to
sites without another late case, so no unplanned site finding can appear. So the evaluation
shows the implementation does what the rule says on records like these, and that harmless
differences of these three kinds are absorbed. It does not show how the rule fares on real
safety data: recoded terms, partial dates, case follow-ups, or two same-term events of one
subject close together (docs/safety-notes.md lists these failure modes, and none of them is
in the set).

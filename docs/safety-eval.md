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
  writes `data/evals/safety/<set>.json`.

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

## Stress set (written independently of the generator)

The scores above show that the code follows the rule. The stress set asks whether the rule
is the right one. It has 16 hand-written pairs of clinical AE rows and safety cases in
`tests/fixtures/sae_stress/`. None was produced by the generator or built with its
normalisation code. For each pair, the ideal outcome (what a safety reviewer would want)
and the outcome predicted under the current rule of safety.sae_reconciler 1.0.0 were
written below and committed before the first run. Record keys are subject/AESEQ or case id.
`?` marks a finding that may or may not appear.

| # | Case | Ideal | Predicted under 1.0.0 |
|---|---|---|---|
| 1 | Recoded preferred term (MedDRA version change) | one pair; low note: terms differ | missing case 201-9001-001/1 + case without SAE SC-ST01A |
| 2 | Clinical onset year and month only | one pair; low note: clinical onset incomplete | missing case 201-9002-001/1 + case without SAE SC-ST02A |
| 3 | Clinical onset year only | one pair; low note: clinical onset incomplete | missing case 201-9003-001/1 + case without SAE SC-ST03A |
| 4 | Case onset year and month only | one pair; low note: case onset incomplete | missing case 201-9004-001/1 + case without SAE SC-ST04A |
| 5 | Follow-up version of the same case id updates the outcome | latest version only: no finding | case without SAE SC-ST05A; outcome mismatch 201-9005-001/1? (which version pairs is not defined) |
| 6 | Follow-up booked as a new case id | one finding: probable duplicate case | outcome mismatch 201-9006-001/1 + case without SAE SC-ST06B |
| 7 | Two syncope SAEs 2 days apart, correct cases (control) | no finding | no finding |
| 8 | Two syncope SAEs 2 days apart, second case carries the first date | AESEQ 1 with SC-ST08B, AESEQ 2 with SC-ST08A; onset mismatch on AESEQ 2 | wrong pairing: outcome mismatch on AESEQ 1 and 2, onset mismatch on AESEQ 2 |
| 9 | Onset 3 days apart | onset mismatch | onset mismatch 201-9009-001/1 |
| 10 | Onset 4 days apart | onset mismatch | missing case 201-9010-001/1 + case without SAE SC-ST10A |
| 11 | Seriousness criterion differs (life-threatening in the case only) | medium finding: criteria differ | no finding |
| 12 | Subject id typo in the case (021 for 012) | one finding: case subject not enrolled, probably 201-9012-012 | missing case 201-9012-012/1 + case without SAE SC-ST12A |
| 13 | Outcome and causality in another vocabulary (RECOVERED, RELATED) | no finding | outcome mismatch + causality mismatch 201-9013-001/1 |
| 14 | Term case and spaces, reported term, same-day date-time (control) | no finding | no finding |
| 15 | Receipt exactly 24 h and 24 h 1 min after awareness | late SC-ST15B only; no site finding | late SC-ST15B only |
| 16 | A case without an awareness date-time, and a real outcome mismatch elsewhere | data-quality finding for SC-ST16A; outcome mismatch 201-9016-002/1 | the run fails (the empty date-time cannot be parsed), so no finding at all for the cut |

### Stress set results

Run on 2026-11-08, after the table above was committed, with safety.sae_reconciler 1.0.0
unchanged (`tests/unit/test_sae_stress.py`; each fixture now records `actual`, and the test
pins it). The agent ran in process on in-memory tables: the stress set tests the rule, not
the sandbox.

| # | Case | Ideal | Predicted | Actual | Ideal met |
|---|---|---|---|---|---|
| 1 | Recoded preferred term | one pair; low note | missing + orphan | missing 201-9001-001/1 + orphan SC-ST01A | no |
| 2 | Clinical onset year and month | one pair; low note | missing + orphan | missing 201-9002-001/1 + orphan SC-ST02A | no |
| 3 | Clinical onset year only | one pair; low note | missing + orphan | missing 201-9003-001/1 + orphan SC-ST03A | no |
| 4 | Case onset year and month | one pair; low note | missing + orphan | missing 201-9004-001/1 + orphan SC-ST04A | no |
| 5 | Follow-up version, same case id | no finding | orphan SC-ST05A; outcome mismatch? | orphan SC-ST05A + outcome mismatch 201-9005-001/1 | no |
| 6 | Follow-up booked as a new case | probable duplicate | outcome mismatch + orphan | outcome mismatch 201-9006-001/1 + orphan SC-ST06B | no |
| 7 | Same term 2 days apart (control) | none | none | none | yes |
| 8 | Same term 2 days apart, copied date | onset mismatch on AESEQ 2 | wrong pairing, 3 findings | onset mismatch /2, outcome mismatch /1 and /2 | no |
| 9 | Onset 3 days apart | onset mismatch | onset mismatch | onset mismatch 201-9009-001/1 | yes |
| 10 | Onset 4 days apart | onset mismatch | missing + orphan | missing 201-9010-001/1 + orphan SC-ST10A | no |
| 11 | Seriousness criterion differs | criteria differ | none | none | no |
| 12 | Subject id typo | not enrolled, probably 201-9012-012 | missing + orphan | missing 201-9012-012/1 + orphan SC-ST12A | no |
| 13 | Other outcome and causality vocabulary | none | 2 mismatches | outcome + causality mismatch 201-9013-001/1 | no |
| 14 | Harmless text and date-time (control) | none | none | none | yes |
| 15 | Receipt at 24 h and 24 h 1 min | late SC-ST15B only | late SC-ST15B | late SC-ST15B | yes |
| 16 | Missing awareness date-time | data-quality finding + outcome mismatch | run fails | run fails (ValueError) | no |

Every prediction held: the rule does what it says. The ideal outcome was met in 4 of 16
cases, three of them controls. For case 5, which version pairs depends on the export's row
order: version 1 comes first in the file, so it pairs. With the rows reversed, only the
orphan remains.

Backlog for safety.sae_reconciler 1.1 (from the misses, highest risk first):

1. **A malformed date-time fails the whole run** (16). One empty AWARE_DTC loses every
   finding of the cut. Parse per case, and raise a data-quality finding instead.
2. **Case versions** (5, 6). Reconcile the latest version of a case only, and flag probable
   duplicate cases (same subject, term and onset).
3. **Pairing by content, not only by date** (8, 10). Within the window, prefer the pairing
   that agrees on outcome and causality. Beyond the window, pair a lone SAE with its lone
   case of the same subject and term as an onset mismatch, not two high findings.
4. **Partial dates** (2, 3, 4). Treat YYYY-MM and YYYY as compatible with any day inside
   them when the pair is otherwise unique, and add a low "onset incomplete" note.
5. **Subject id typos** (12). For a case whose subject is not enrolled, look for the same
   site, term and onset under a near subject id (one transposition or one digit).
6. **Coding** (1). Use the MedDRA hierarchy (same HLT or LLT) or the verbatim term as a
   fallback key, with a low "terms differ" note.
7. **Vocabularies** (13). Map outcome and causality to one controlled vocabulary before
   comparing; the mapping (for example PROBABLE as related) is a sponsor decision.
8. **Seriousness criteria** (11). Compare them, as a medium finding.

Each of these is measured again on this set by the next version, with new ideal and
predicted columns written before its run.

## Sealed holdout stress set (for 1.1)

The 16 cases above are now development data: they shaped the 1.1 backlog. A second set of
16 hand-written cases is in `tests/fixtures/sae_stress_holdout/`. It covers the same
failure families with different values and in combinations, and 4 of its cases are clean
controls. It was written and committed before any 1.1 code, and 1.1 is run on it once, at
the end. The ideal outcome is in the 1.1 check vocabulary:

- sae_data_quality: record `<record>#<FIELD>`;
- sae_probable_duplicate: record `<case>+<case>`;
- sae_partial_date, seriousness_criteria_mismatch and sae_term_mismatch: the clinical
  record;
- sae_subject_mismatch: the case.

| # | Case | Control | Ideal |
|---|---|---|---|
| 1 | Clinical onset year-month; follow-up version (listed first) updates outcome | no | sae_partial_date 201-9201-003/2 |
| 2 | Subject typo (one digit) plus recoded term | no | sae_subject_mismatch SC-HO02A |
| 3 | Three seizure SAEs; one case dated 4 days late, next to another event | no | onset_mismatch 201-9203-005/5 |
| 4 | Causality in other words plus a real outcome change | no | outcome_mismatch 201-9204-011/1 |
| 5 | Outcome and causality in other words only | yes | none |
| 6 | Two different SAEs of one subject on the same day | yes | none |
| 7 | Term case and space, date-time onset, receipt at 23 h 59 min | yes | none |
| 8 | Clinical onset empty; another case's receipt in a non-ISO format | no | sae_data_quality 201-9208-004/1#AESTDTC, SC-HO08B#RECEIPT_DTC |
| 9 | Follow-up booked as a new case, onset one day later | no | sae_probable_duplicate SC-HO09A+SC-HO09B |
| 10 | Onset 6 days apart; onset 9 days apart (lone pairs) | no | onset_mismatch 201-9210-002/1, 201-9210-003/1 |
| 11 | Death flag set in the clinical database only | no | seriousness_criteria_mismatch 201-9211-001/1 |
| 12 | Subject typo with two candidate subjects | no | missing 201-9212-011/1, missing 201-9212-019/1, case without SAE SC-HO12A |
| 13 | Case onset year only, plus causality and outcome in other words | no | sae_partial_date 201-9213-007/1 |
| 14 | Recoded term for a subject with a second SAE without a case | no | sae_term_mismatch 201-9214-012/1, missing 201-9214-012/2 |
| 15 | Case outcome outside any terminology | no | sae_data_quality SC-HO15A#OUTCOME |
| 16 | Stroke with sequelae; identical follow-up version three weeks later | yes | none |

### Targets for 1.1 on the sealed holdout

Written before 1.1 is first run on the sealed set. "Ideal met" means the agent's findings
(check and record) are exactly the ideal set in the table above.

| Measure | Target |
|---|---|
| Controls (4) with the ideal met | 4 of 4 |
| Non-control cases (12) with the ideal met | at least 10 of 12 |
| Cases where the run raises | 0 |

The set is run once (`tests/unit/test_sae_stress_holdout.py`, added with the results), and
the agent is not changed after it. Misses go to the backlog for the local-model step (3b) or
a later version.

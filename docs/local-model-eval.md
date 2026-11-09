# Local model evaluation (phase 3 step 3b)

The targets, the metrics and the rule that chooses the model are written here before any
candidate is run on the term pairs. They are not changed in response to the results.
Results are added below the line "Results" in a later commit.

## What is evaluated

safety.term_adjudicator 1.0.0, exactly as it runs on findings: the same system prompt
(meridian.agents.safety.term_adjudicator.SYSTEM_PROMPT), the same request options
(temperature 0, seed 20261109, num_ctx 4096, num_predict 256, think false, format json),
the same validator 1.0.0, and the same path: one job per model in the safety.local_model
sandbox, through the policy proxy to Ollama on 172.17.0.1:11434.

Candidates, pinned by the digest /api/tags reports (config/default.toml):

| Model | Digest (first 12) | Parameters | Quantisation |
|---|---|---|---|
| qwen3.5:4b-q4_K_M | d8b0f5e9760c | 4.2B | Q4_K_M |
| phi4-mini:3.8b-q4_K_M | 78fad5d182a7 | 3.8B | Q4_K_M |
| granite4:3b | 89962fcc7523 | 3.4B | Q4_K_M |

## The dev set

data/term_pairs/dev.jsonl, 61 pairs written for this step, each with a preferred and a
verbatim term per record, a label (same: yes; different: no) and a category:

* same event (30): word_order (5), spelling (6, British and American), abbreviation (6),
  specificity (10, a more and a less specific term for one condition), verbatim (3, the
  same preferred term with different verbatim words);
* look-alike different events (26): opposite (13, for example hypotension and
  hypertension, hyperkalaemia and hypokalaemia, tachycardia and bradycardia) and
  same_organ (13, the same organ with a different condition);
* plainly different events (5): different.

## Metrics

Per model, over every pair:

* accuracy: the share of pairs whose verdict equals the label. "unsure", a rejected
  answer and an error all count as not correct.
* "no" recall on look-alikes: the share of the 26 look-alike pairs answered "no".
* wrong "yes" on look-alikes: how many look-alike pairs were answered "yes" (the primary
  safety number), and wrong "yes" on all different pairs.
* "unsure" rate, and the share of answers rejected by the validator (with reason codes).
* latency per call: median and 95th percentile over warm calls, and the first call (it
  includes loading the model).
* VRAM: size and size_vram from Ollama's /api/ps right after the job, read on the host,
  and whether the model ran fully on the GPU or partly on the CPU at num_ctx 4096.

## Targets

Primary safety rule: on look-alike different events the chosen model says "yes" at most
once in the dev set. A wrong "yes" is worse than "unsure": "unsure" sends a person to
the records, a wrong "yes" invites them to close a real discrepancy.

For the chosen model on the dev set, also:

* accuracy at least 0.80;
* "no" recall on look-alikes at least 0.85;
* "unsure" rate at most 0.15;
* rejected answers at most 5% (3 pairs).

## How the model is chosen

1. Only models that meet the primary rule and reject at most 5% of answers are eligible.
2. Among those, the highest accuracy wins.
3. Ties: fewer wrong "yes" on all different pairs, then the model that runs fully on the
   GPU at num_ctx 4096, then the lower median latency.
4. If no model is eligible, none is chosen: the adjudicator is not run on findings, and
   this file says so.

A chosen model that misses one of the other targets is still chosen (the rule above
decides); the miss is reported.

## The sealed set

Written by the user and kept outside the repository. It is run once, with the chosen
model and the prompt exactly as committed before the run; nothing is changed afterwards
in response to it, and the file is never copied into the repository or the docs. The
same metrics are reported, with every error. The primary rule for the sealed set: wrong
"yes" on look-alikes at most max(1, 5% of its look-alike pairs).

## The local narrator

safety.local_narrator 1.0.0 with the chosen model, on every current safety finding of
DC-BASE and the dev cuts: counts by status and by rejection reason code, and five
accepted summaries next to their evidence for review. No pass mark is set in advance:
every accepted summary has passed validator 1.1.0 with the local rules, and the review
decides whether the summaries are useful.

## Step 3b2: abstain on disagreement (targets, written before the run)

No model said "unsure" in step 3b, and every sealed error was a pair labelled "unsure".
Two ways to get abstention are measured on the dev set only (the first sealed set is
spent):

* **1.1.0, agreement**: phi4-mini:3.8b-q4_K_M and qwen3.5:4b-q4_K_M (think false) are
  asked the same question with the 1.0.0 prompt; "yes" only if both say yes, "no" only if
  both say no, otherwise "unsure" (an answer that fails validation agrees with nothing).
* **unsure prompt**: phi4-mini alone, with rule 3 of the prompt extended to "unsure when
  one term may be a cause, a symptom or a complication of the other"
  (term_adjudicator.UNSURE_PROMPT). Also reported, for information only: the agreement
  rule with the unsure prompt on both models (two changes at once; not a candidate).
* **1.0.0**, phi4-mini alone with the 1.0.0 prompt, is the baseline, rerun on the same set.

The dev set gains 14 pairs labelled "unsure" (category related: one term may be a cause,
symptom or complication of the other, for example PYREXIA and SEPSIS), none of them in
the sealed set. It now has 75 pairs: 30 yes, 31 no (26 look-alikes), 14 unsure. On a pair
labelled "unsure" only "unsure" is correct.

Targets on dev:

* **wrong "yes"** (the primary number): "yes" on a look-alike plus "yes" on a pair
  labelled "unsure", at most one;
* reported without a pass mark: the "unsure" rate, "unsure" on the 14 unsure-labelled
  pairs, accuracy, rejected answers, latency, and the cost of abstention: how many pairs
  1.0.0 answered correctly that the variant does not (and how many it gains).

What ships:

1. The agreement rule (1.1.0), if it meets the wrong "yes" target.
2. The unsure prompt instead, only if it meets the target and the agreement rule does
   not, or it has fewer wrong "yes", or the same number and costs at least 3 fewer correct
   answers against 1.0.0.
3. If neither meets the target, the agreement rule ships (it cannot say "yes" more often
   than either model alone) and the miss is reported.

A second sealed set, if the user gives one, is run once at the end with what ships.

## Results

### Dev term pairs (2026-11-09)

`meridian eval terms` (one job per model in the safety.local_model sandbox; per-pair
results in var/evals/term_pairs.json, not committed):

| Model | Accuracy | "no" recall, look-alikes | Wrong "yes", look-alikes | Wrong "yes", all different | "unsure" | Rejected | Latency median / p95 / first | VRAM at num_ctx 4096 |
|---|---|---|---|---|---|---|---|---|
| granite4:3b | 0.951 | 0.923 | 2 | 2 | 0 | 0 | 458 / 604 / 5373 ms | 2.5 GB, 100% GPU |
| phi4-mini:3.8b-q4_K_M | 0.967 | 0.962 | 1 | 1 | 0 | 0 | 1000 / 1172 / 8754 ms | 3.2 GB, 16% CPU / 84% GPU |
| qwen3.5:4b-q4_K_M | 0.934 | 0.962 | 0 | 0 | 0 | 3 | 2589 / 3224 / 12016 ms | 3.7 GB, 42% CPU / 58% GPU |

Errors:

* granite4: dev-020 (ARRHYTHMIA and ATRIAL FIBRILLATION, label yes) answered no;
  dev-045 (GASTRITIS and GASTRIC ULCER) and dev-050 (ATRIAL FIBRILLATION and ATRIAL
  FLUTTER) answered yes.
* phi4-mini: dev-021 (INFECTION and CELLULITIS, label yes) answered no; dev-050 answered
  yes.
* qwen3.5: dev-020 answered no; dev-003 and dev-023 rejected (reason_too_long), dev-054
  rejected (too_many_sentences).

Choice, by the rule above: granite4 is not eligible (two wrong "yes" on look-alikes).
phi4-mini and qwen3.5 are; phi4-mini is more accurate (0.967 against 0.934), so
**phi4-mini:3.8b-q4_K_M** is chosen. It meets every target: accuracy 0.967, "no" recall
0.962, "unsure" 0, rejected 0, one wrong "yes" on look-alikes. Two things the rule did not
weigh: qwen3.5 made no wrong "yes" at all but is 2.6 times slower and half on the CPU,
and no model ever said "unsure", so the "unsure" path is untested by this set.

### Stress cases 2 and 14 of the sealed reconciliation set

`scripts/stress_assessments.py` with phi4-mini (reconciler 1.1.1 in memory, a scratch
ledger, one real sandbox pass):

* Case 2 (typo_plus_recoded_term): the reconciler raises sae_missing_in_safety_db for
  201-9202-019 and case_without_clinical_sae for SC-HO02A, whose subject is 201-9202-018.
  The subjects differ, so they are no candidate and get no assessment.
* Case 14 (recoded_term_two_unmatched): one candidate, the missing case of AESEQ 1
  (HYPERSENSITIVITY) with case SC-HO14A (DRUG HYPERSENSITIVITY), one day apart; AESEQ 2
  (NAUSEA, 18 days later) is outside the window. Assessment: accepted, **yes**, "Both
  terms describe the same condition of increased sensitivity to a substance."

### The local narrator on DC-BASE and the dev cuts

77 current safety findings (reconciler 1.1.1; DC-BASE has none). The first pass sent all
77 in one exec and the gateway cut it after about 12 minutes ("exec relay closed before
the command reported an exit status"): 77 error narratives, retried. With jobs of 16:

* accepted 16, rejected 61, error 0;
* rejection reasons (a narrative can have several): missing_site 60, number_not_displayed
  4, supporting_as_headline 4, date_not_displayed 3, bad_json 1;
* accepted by check: sae_late_reporting_site 8 of 8, late_sae_report 4 of 28,
  outcome_mismatch 3 of 9, onset_mismatch 1 of 8; none of sae_missing_in_safety_db,
  case_without_clinical_sae or causality_mismatch (8 each);
* median latency 2756 ms.

phi4-mini rarely writes "Site <id>" for a subject-level finding, whose title names the
subject and not the site; the validator rejects that as it does for the cloud narrator.
The prompt was not changed in response.

### The sealed set (once, 2026-11-09)

Run once with phi4-mini:3.8b-q4_K_M (digest 78fad5d182a7), the prompt, options and
validator as committed; nothing was changed afterwards. The file stays outside the
repository; only the numbers are here. It has 33 pairs labelled yes 15, no 11 and
unsure 7, and one term per record, so each term was sent as both the preferred and the
verbatim term. Look-alikes: the 10 "no" pairs other than the unrelated control.

| Measure | Result | Target |
|---|---|---|
| Wrong "yes" on look-alikes | 0 of 10 | at most 1 (primary rule): met |
| "no" recall on look-alikes | 1.0 | at least 0.85: met |
| Accuracy | 0.788 (26 of 33) | at least 0.80: missed |
| "unsure" answers | 0 | at most 0.15: met |
| Rejected answers | 0 | at most 5%: met |
| Latency, median of warm calls / first | 1010 / 7462 ms | |

All seven errors are pairs labelled "unsure": six were answered "no" and one "yes" (a
general and a local form of one skin reaction). The model never says "unsure", which
is what costs the accuracy target; it never said "yes" to a pair labelled "no".

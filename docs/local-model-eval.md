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

### Step 3b2: the agreement rule and the unsure prompt on dev (2026-11-09)

`scripts/term_agreement_eval.py`: 75 dev pairs (30 yes, 31 no of which 26 look-alikes,
14 unsure), each model with each prompt in its own jobs in the safety.local_model
sandbox; the agreement rows are combined from the single-model runs exactly as the
agent combines answers. Wrong "yes" counts "yes" on look-alikes and on pairs labelled
"unsure". Cost: pairs 1.0.0 answered correctly that the variant does not (lost), and the
reverse (gained).

| Variant | Correct (accuracy) | Wrong "yes" | "unsure" answers | "unsure" on the 14 unsure-labelled | Rejected | Lost / gained vs 1.0.0 | Median latency |
|---|---|---|---|---|---|---|---|
| 1.0.0, phi4-mini | 59 (0.787) | 1 (dev-050) | 0 | 0 | 0 | | 1013 ms |
| qwen3.5 alone | 57 (0.760) | 0 | 0 | 0 | 3 | 4 / 2 | 2695 ms |
| **1.1.0 agreement** | 55 (0.733) | **0** | 6 (0.08) | 0 | 0 | 4 / 0 | 3718 ms (both) |
| unsure prompt, phi4-mini | 60 (0.800) | 1 (dev-050) | 0 | 0 | 0 | 0 / 1 | 1088 ms |
| unsure prompt, qwen3.5 (information) | 59 (0.787) | 0 | 0 | 0 | 1 | 2 / 2 | 2918 ms |
| agreement + unsure prompt (information) | 58 (0.773) | 0 | 3 (0.04) | 0 | 0 | 2 / 1 | 4179 ms (both) |

Where the agreement rule abstains (6 pairs):

* dev-050 (ATRIAL FIBRILLATION and ATRIAL FLUTTER, label no): phi4-mini yes, qwen3.5 no.
  This is 1.0.0's one wrong "yes", now "unsure".
* dev-021 (INFECTION and CELLULITIS, label yes): phi4-mini no, qwen3.5 yes (1.0.0 was
  wrong here too).
* Lost, label yes: dev-020 (qwen3.5 no); dev-003 and dev-023 (qwen3.5's answer rejected,
  reason_too_long). Lost, label no: dev-054 (qwen3.5 rejected, too_many_sentences).

Three of the four lost answers come from qwen3.5 answers that fail the validator, not
from disagreement.

The unsure-labelled pairs: every variant answered all 14 "no" (cause, symptom or
complication, for example PYREXIA and SEPSIS). That is not a wrong "yes", but neither
the agreement rule nor the explicit instruction makes a model say "unsure" there: the
unsure prompt changed no "no" to "unsure" on any pair. Abstention comes only from
disagreement.

Decision, by the rule written above: the agreement rule meets the target (0 wrong
"yes"); the unsure prompt also meets it (1) but has more wrong "yes" than the agreement
rule, so **1.1.0 agreement ships**, with the 1.0.0 prompt. Its cost: 4 correct answers
of 75 turned "unsure", and calls take both models' time (about 3.7 s per pair).

### Step 3b2: safety.local_narrator 1.0.1 (2026-11-09)

`meridian agents narrate-local` on every current safety finding (reconciler 1.1.1): 158
findings, now including the holdout and clean cuts. One job of 16 failed on a gateway
HTTP/2 error ("stream closed because of a broken pipe") and was retried on the next
pass. Final outcomes, with 1.0.0 on the 77 findings it saw in step 3b:

| | Findings | Accepted | Rejected | Rejection reasons (a narrative can have several) |
|---|---|---|---|---|
| 1.0.0 (step 3b) | 77 | 16 | 61 | missing_site 60, number_not_displayed 4, supporting_as_headline 4, date_not_displayed 3, bad_json 1 |
| 1.0.1, the same 77 | 77 | 66 | 11 | filler_sentence_severity 9, too_many_sentences 2, filler_sentence_discrepancy 2 |
| 1.0.1, all | 158 | 135 | 23 | filler_sentence_severity 16, filler_sentence_discrepancy 5, too_many_sentences 4 |

On the same 77: 59 went from rejected to accepted, 9 from accepted to rejected, 7 stayed
accepted, 2 stayed rejected. Accepted by check, all 158: sae_missing_in_safety_db 16/16,
late_sae_report 57/60, causality_mismatch 15/16, onset_mismatch 16/17,
outcome_mismatch 16/17, case_without_clinical_sae 14/16, sae_late_reporting_site 1/16.

* missing_site is gone: the opening carries the site.
* The site-level check went from 8/8 to 1/16. Re-sent through the sandbox (nothing
  recorded), its answers end "This is considered a high severity finding.", which the
  new rule rejects as intended. The prompt says not to; phi4-mini does it anyway.
* The other filler rejections read "This discrepancy was identified through a
  reconciliation process." and "This discrepancy is supported by the recorded date of
  the AE.": filler, rightly rejected.
* Temperature 0 and a fixed seed do not make answers repeatable: re-sending 7 rejected
  requests gave different text for 2 (both then passed). The ledger keeps what was
  validated at the time.

What the validator does not catch (in accepted texts): a date given the wrong meaning
("This discrepancy was noted on 25 May 2013", the onset), a raw evidence selector copied
into prose ("CASEID=SC-A4C2754345; VERSION=1; ..."), and the template summary copied
whole.

### Step 3b2: the term path end to end (2026-11-09)

`meridian safety inject --set terms` wrote 10 cuts with 15 term faults (7 recoded_term,
8 lookalike_term); `meridian safety run --set terms` ran safety.sae_reconciler 1.1.1 in
OpenShell sandboxes; `meridian agents adjudicate` ran safety.term_adjudicator 1.1.0 in
the safety.local_model sandbox (both models, one job each).

* Findings: 15 sae_term_mismatch, precision 1.00 and recall 1.00 against the ground truth
  (`meridian safety eval --set terms`), nothing else raised.
* Assessments: 15, all accepted.

| Fault | Pair (clinical / case) | n | phi4-mini | qwen3.5 | Verdict |
|---|---|---|---|---|---|
| recoded_term | PRURITUS / PRURITUS GENERALISED | 2 | yes | yes | yes |
| recoded_term | PRURITUS / PRURITUS GENERALISED | 1 | no | yes | unsure |
| recoded_term | HEADACHE / TENSION HEADACHE | 1 | yes | yes | yes |
| recoded_term | APPLICATION SITE PRURITUS / ADMINISTRATION SITE PRURITUS | 1 | no | yes | unsure |
| recoded_term | APPLICATION SITE DERMATITIS / ADMINISTRATION SITE DERMATITIS | 1 | no | yes | unsure |
| recoded_term | APPLICATION SITE ERYTHEMA / ADMINISTRATION SITE ERYTHEMA | 1 | no | no | **no** |
| lookalike_term | PRURITUS / PRURIGO | 4 | yes | no | unsure |
| lookalike_term | ERYTHEMA / ERYTHEMA NODOSUM | 2 | no | no | no |
| lookalike_term | HYPERHIDROSIS / HYPOHIDROSIS | 1 | no | no | no |
| lookalike_term | NASOPHARYNGITIS / LARYNGITIS | 1 | no | no | no |

* recoded_term (expected yes): yes 3, unsure 3, no 1. lookalike_term (expected no): no 4,
  unsure 4, yes 0. Wrong "yes": 0. phi4-mini alone (1.0.0) would have said "yes" to all
  4 PRURITUS / PRURIGO look-alikes.
* The one wrong "no": both models read ADMINISTRATION SITE ERYTHEMA as another event.
  That is the safe direction (the finding stays open for a person), but it is wrong.
* PRURITUS is the commonest serious AE term after augmentation, so the seeded draw
  picked PRURITUS pairs for 7 of the 15 faults.
* Who sees it: the medical monitor and the clinical operations lead see the assessment
  on each finding page, with both raw answers, models and digests; the toxicologist
  gets 404 for these findings and so never sees their assessments (unchanged rule: an
  assessment is shown only when every finding it names is).

### Step 4a: safety.term_adjudicator 1.1.1 on dev (2026-11-10)

Validator 1.1.0 accepts a reason of up to 2 sentences and 300 characters (1.0.0: one
sentence, 200): in the 3b2 run three qwen3.5 answers (dev-003, dev-023 reason too long,
dev-054 two sentences) were rejected only for their length. The prompt still asks for one
sentence of at most 200 characters, and the agreement rule is unchanged. Rerun with
`uv run python scripts/term_adjudicator_rerun.py` (both models, the agent's prompt, the
local model sandbox), against the 1.1.0 results of 3b2:

| | 1.1.0 (3b2) | 1.1.1 |
|---|---|---|
| correct of 75 | 55 (0.733) | 57 (0.760) |
| wrong "yes" | 0 | 0 |
| "unsure" verdicts | 6 (0.080) | 4 (0.053) |
| qwen3.5 answers rejected | 3 | 0 |
| phi4-mini correct alone | 59 | 59 |
| qwen3.5 correct alone | 57 | 59 |
| median latency per pair | 3718 ms | 3711 ms |

Changed verdicts: dev-003 (yes) unsure to yes, dev-054 (no) unsure to no, both now
right. dev-023 is still unsure: qwen3.5's answer now passes, but the two models disagree
on it. All 14 pairs labelled unsure still get "no" from both models. As in 3b2, answers
are not guaranteed repeatable, so a second run could move a pair either way.

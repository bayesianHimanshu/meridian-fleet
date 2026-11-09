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

## Results

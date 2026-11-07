# rbqm.narrator 1.0.0: evaluation on DC-BASE, DC-RBQM-CORE and holdout 2

Run on 2026-11-07 with `meridian agents narrate --cut DC-BASE --cut DC-RBQM-CORE --cut
DC-51448ab6`, in OpenShell sandboxes from policies/rbqm.narrator.yaml with the real
meridian-anthropic provider, after `meridian sandbox conformance --policy rbqm.narrator`
passed on the same image (ledger seq 463 to 467). Model claude-haiku-5-5, max_tokens 300,
thinking disabled, no temperature (see "Problems"). Default budget of 20 calls per pass.
The numbers come from `uv run python scripts/narrator_eval.py`.

## Counts

53 findings from rbqm.kri_monitor, rbqm.qtl_detector and rbqm.csm on the three cuts (19,
29 and 5), every version of those agents included: a finding is narrated once whatever
version raised it.

| Status | Events |
| --- | ---: |
| accepted | 53 |
| rejected | 0 |
| refused | 0 |
| error | 1 |

* The error is RBQM-0001's first call: HTTP 400 "`temperature` is deprecated for this
  model." It was recorded, and the next pass sent it again without temperature (accepted).
* No rejections and no refusals. No finding holds a subject id or USUBJID, so the
  de-identification check had nothing to stop; and every reply was valid JSON within the
  limits, naming the site, with only numbers from the input. The rejection paths are
  therefore exercised only by the unit tests (tests/unit/test_narrator.py), not here.
* Budget carry-over worked as designed: the passes narrated 20, 20 and 12 findings (32 and
  then 12 waiting), and a fourth pass found nothing pending.

Tokens: 46,115 input and 6,035 output over 53 successful calls (about 870 and 114 per
call). At $0.10 and $0.50 per million tokens (claude-haiku-5-5 list price from the cached
model table, prompts under 100K tokens) that is about $0.008 in all. Median latency inside
the sandbox 1.2 s, maximum 1.5 s. Five sandboxes (one per pass).

## Five accepted narratives, next to their evidence

**RBQM-0027** (DC-BASE, under_reporting, site 1005). Summary: "Site 1005 has 27 adverse
events over 2075 subject-days; the rest of the study implies 70.2. Poisson p = 3.35e-09,
4.35e-08 after Bonferroni over 13 sites." Evidence: observed 27, expected 70.23, rate
1.301 per 100 subject-days (threshold 3.384), adjusted p 4.349e-08 (threshold 0.01).
> Site 1005: fewer adverse events than expected. Site 1005 recorded 27 adverse events over
> 2075 subject-days, while the rest of the study implies about 70.2. The difference is
> statistically significant (Poisson p = 3.35e-09, or 4.35e-08 after adjusting for 13
> sites).

**RBQM-0034** (DC-RBQM-CORE, kri_breach, site 1016). Summary: "Site 1016 has 18 protocol
deviations over 3945 subject-days; the rest of the study implies 5.1. Poisson p = 6.95e-06,
9.03e-05 after Bonferroni over 13 sites." Evidence: observed 18, expected 5.095, adjusted p
9.033e-05 (threshold 0.01).
> Site 1016: 18 protocol deviations, well above the expected count. Site 1016 recorded 18
> protocol deviations over 3945 subject-days, where the rest of the study implies about
> 5.1. The difference is statistically significant even after adjusting for testing across
> 13 sites (adjusted p = 9.03e-05).

**RBQM-0037** (DC-BASE, qtl_breach, study-wide). Summary: "144 of 254 treated subjects
(56.7%) discontinued before completion; the QTL is 61.7% and approaching starts at 90% of
it." Evidence: rate 0.5669 (threshold 0.617), share of limit 0.9188 (threshold 0.9).
> Study-wide premature discontinuation is approaching its QTL limit. 144 of 254 treated
> subjects (56.7%) discontinued before completion. The QTL is 61.7%, and the rate is
> approaching it, having reached 90% of the limit.

**RBQM-0039** (DC-RBQM-CORE, site_data_anomaly, site 1001). Summary: "Site 1001 has a much
smaller spread than the other sites for SYSBP, PULSE (robust z of log SD below -3.5 and SD
below 0.6 of the median site). Terminal digits are attached as supporting evidence only."
Evidence: SYSBP log SD z -5.459, SD ratio 0.4508 (threshold 0.6); PULSE log SD z -4.35, SD
ratio 0.5021.
> Site 1001: vital signs vary unusually little. Site 1001's SYSBP and PULSE readings have
> much smaller spread than other sites, with SD below 0.6 of the median site. Terminal
> digits are noted only as supporting evidence.

**RBQM-0021** (DC-RBQM-CORE, site_data_anomaly, site 1013). Summary: terminal digits
differ for DIABP, PULSE and SYSBP, Bonferroni over 78 tests. Evidence: SYSBP and DIABP
terminal-digit p 6.2e-78 and 4.8e-83 (threshold 0.01), terminal zero share 0.9164 for
both; PULSE p 1.7e-11, zero share 0.2651.
> Site 1013: vital sign readings look statistically unusual. Site 1013's systolic and
> diastolic blood pressure and pulse readings show terminal digit patterns that differ from
> the other sites, with p values below 0.01 after adjustment across 78 tests. The systolic
> and diastolic readings also have a terminal zero share of about 0.92, which the finding
> reports without further interpretation.

Every severity a narrative mentions matches the finding's severity (checked for all 53).

## Passed validation but reads wrong

* **RBQM-0035** (site 1004, entry lag 14.0 days, threshold 7): "The lag is therefore twice
  the limit set for this indicator." Arithmetically right, but a computed ratio, which the
  prompt forbids; the number check cannot see a number written as a word.
* **RBQM-0006** (site 1009): "The summary notes that 49% of systolic readings, 39% of
  diastolic readings and 20% of pulse readings end in zero." The numbers are right (from
  the evidence), but the summary says no such thing.
* **RBQM-0010** (site 1015) and **RBQM-0001** (site 1001): they present the terminal zero
  share (about 15%, and 12%, 11%, 13%) as if it were the anomaly ("which the finding
  describes as a terminal-digit pattern"). The test is on the whole last-digit
  distribution; those zero shares are close to the 10% a uniform last digit gives, so a
  reader is led to the wrong reason. RBQM-0001 also says "adjusted p values of 0": two of
  the evidence values are 0.0 only because the p value underflowed.
* **RBQM-0037** (above): "having reached 90% of the limit". The rate is at 91.9% of the
  limit; 90% is where "approaching" starts. Not false, but imprecise.
* **RBQM-0042** (site 1004): "The lag is measured as the gap between visits and their entry
  into the data system." A definition the finding does not state; correct here, but it is
  the model adding content.

None of these changes what the finding says, and none would mislead about which site or
which number; three of them (RBQM-0001, 0006, 0010) would mislead about why the site was
flagged. Validation catches format, numbers, site, dash and ids; it cannot check
attribution ("the summary notes"), interpretation, or numbers in words. Candidates for
1.1.0: reject number words (twice, half, double), reject "the summary notes/says" unless
quoting it, and drop the terminal zero share from a terminal-digit finding's input or label
it as context.

The 53 findings include near-duplicates from different detector versions on the same cut
(for example RBQM-0036, 0043 and 0085, all "Site 1008: open queries are ageing" on
DC-RBQM-CORE). That is how the findings were recorded, not a narrator issue; they were
narrated consistently.

# Step 4: validator evaluation (rbqm.narrator 1.1.0)

## Targets, stated before the labelled set was built or any metric computed

Committed before data/narrator_eval/labelled.jsonl existed:

1. All 5 real_bad narratives rejected by validator 1.1.0.
2. Recall on mutated narratives at least 0.9 (validator 1.1.0).
3. At most 1 false rejection on real_good (validator 1.1.0).

Results are reported below as they are, whether or not a target is met. Known in advance:
the labelled narratives were written by narrator 1.0.0 without display strings, so the
exact display match of validator 1.1.0 is measured against text that was never asked to
use displays.

## The labelled set

data/narrator_eval/labelled.jsonl, built by `scripts/build_narrator_labels.py` (seed
20261107; labels and one-line justifications in docs/narrator-labels.md, assistant-judged):

* real_bad: the 5 known-bad 1.0.0 narratives, with the reason code each should get.
* real_good: 47 of the other 48 accepted 1.0.0 narratives. RBQM-0042 is excluded as
  doubtful (it adds a definition of entry lag the finding does not state).
* mutated: 240 bad variants of the 47 real_good narratives (wrong_number 47,
  unbacked_ratio 47, supporting_headline 47, swap_site 44, flip_direction 43, zero_p 12;
  a type is skipped where it cannot apply, for example no p value in the text).

Each validator is run on the input its own narrator version sees: 1.0.0 on raw values,
1.1.0 on described evidence (the legacy findings described the same way as new ones, but
without the ratio items, which only the new detectors add).

## Validator 1.0.0 and 1.1.0 side by side

`uv run python scripts/narrator_validator_eval.py`:

| Metric | validator 1.0.0 | validator 1.1.0 |
| --- | ---: | ---: |
| recall on bad, overall | 0.49 (121/245) | 1.00 (245/245) |
| recall, real_bad | 0.00 (0/5) | 1.00 (5/5) |
| recall, mutated | 0.50 (121/240) | 1.00 (240/240) |
| recall, wrong_number | 1.00 (47/47) | 1.00 (47/47) |
| recall, flip_direction | 0.00 (0/43) | 1.00 (43/43) |
| recall, swap_site | 1.00 (44/44) | 1.00 (44/44) |
| recall, unbacked_ratio | 0.11 (5/47) | 1.00 (47/47) |
| recall, supporting_headline | 0.53 (25/47) | 1.00 (47/47) |
| recall, zero_p | 0.00 (0/12) | 1.00 (12/12) |
| recall, wrong_number, base accepted | 1.00 (47/47) | 1.00 (19/19) |
| recall, flip_direction, base accepted | 0.00 (0/43) | 1.00 (17/17) |
| recall, swap_site, base accepted | 1.00 (44/44) | 1.00 (16/16) |
| recall, unbacked_ratio, base accepted | 0.11 (5/47) | 1.00 (19/19) |
| recall, supporting_headline, base accepted | 0.53 (25/47) | 1.00 (19/19) |
| recall, zero_p, base accepted | 0.00 (0/12) | n/a (0) |
| false rejections on real_good | 0 of 47 | 28 of 47 |
| bad: rejected for the expected reason | 91 | 243 |
| bad: rejected for another reason only | 30 | 1 |
| bad: rejected (no expected code) | 0 | 1 |
| bad: accepted | 124 | 0 |

"base accepted" restricts a mutation type to narratives whose unmutated version the same
validator accepted, so a rejection there can only come from the mutation. 1.1.0 accepts
only 19 of the 47 bases, so its "mutated" recall of 1.00 is partly carried by rejections
the bases already had; on the 19 clean bases it is still 1.00 for every type (zero_p has
no clean base: every narrative with a p value was already rejected for its precision).

### Against the targets

| Target (stated before the run) | Result | |
| --- | --- | --- |
| All 5 real_bad rejected | 5 of 5 | met |
| Recall on mutated at least 0.9 | 1.00 (240/240); 1.00 on clean bases | met |
| At most 1 false rejection on real_good | 28 of 47 | **missed** |

### Which rule caught each real_bad case (validator 1.1.0)

| Case | Problem | Codes given | Expected code |
| --- | --- | --- | --- |
| RBQM-0001 | "p values of 0"; zero shares framed as the anomaly | zero_p_value, number_not_displayed | zero_p_value: yes |
| RBQM-0006 | "The summary notes ..." for evidence figures | misattribution, number_not_displayed | misattribution: yes |
| RBQM-0010 | 15% zero share framed as the pattern | number_not_displayed | none targets the framing: caught by the rounded "15%" only |
| RBQM-0035 | "twice the limit" | unbacked_ratio | unbacked_ratio: yes |
| RBQM-0037 | "having reached 90% of the limit" | threshold_restated | threshold_restated: yes |

Validator 1.0.0 accepted all five.

### Why 1.1.0 rejects 28 good narratives

* 24 for number_not_displayed. The 1.0.0 narratives round ("about 56%" for a display of
  55.8%, "40 days" for "40.0 days", "about 134" for "134.0") or copy p values from the
  1.0.0 summaries at 3 significant digits ("4.35e-08" where the display, at 2, is
  "4.3e-08"). The exact display match rejects all of them by design: the rule makes the
  narrator copy displays, and these narratives were never asked to.
* 4 for misattribution (RBQM-0004, 0009, 0015, 0016): each says "the summary notes ..."
  about something the summary does say. The rule rejects any citation of the summary, true
  or not, so it cannot separate these from RBQM-0006.

So the false-rejection target is missed, and the miss is structural, not noise: measured
on 1.0.0-style text, the strict rules trade a large false rejection rate for full recall.
What matters in production is whether 1.1.0's own narratives (written to copy displays)
pass; the live re-run below is that test, on 11 findings only.

One bug was found by this evaluation and fixed before the numbers above:
direction_contradicted split clauses at the decimal point of "1.57e-06", so a "below" in
RBQM-0041 lost its p value context. The fix (punctuation ends a clause only before a
space) changed no total; RBQM-0041 is still rejected for number_not_displayed. A
regression test covers it. No rule or display was tuned to the labels otherwise.

Caveats: the labels are mine, not a domain expert's; the mutations and validator 1.1.0
have the same author, so mutated recall is optimistic; direction_contradicted is a rule
not in the step 4 specification, added because the evidence now carries a direction and
flip_direction is one of the mutation types.

## Live re-run: narrator 1.1.0 on the new detector versions

After `meridian sandbox conformance --policy rbqm.narrator` passed on the rebuilt image
(ledger seq 771 to 775), `meridian agents narrate` on DC-BASE, DC-RBQM-CORE and
DC-51448ab6 narrated the 11 findings of kri_monitor 2.2.0, qtl_detector 2.1.0 and csm
2.1.0 in one pass (seq 776 to 786): 10 accepted, 1 rejected, no refusal, no error.
`uv run python scripts/narrator_rerun_table.py`:

| Cut | Kind | Site | Check | 1.1.0 finding | 1.1.0 status | 1.0.0 counterparts |
| --- | --- | --- | --- | --- | --- | --- |
| DC-BASE | kri_breach | 1008 | deviations | RBQM-0134 | rejected (too_many_sentences) | RBQM-0040, 0082 accepted |
| DC-RBQM-CORE | kri_breach | 1004 | data_entry_lag | RBQM-0136 | accepted | RBQM-0035 (known bad), 0042, 0084 accepted |
| DC-RBQM-CORE | kri_breach | 1008 | query_aging | RBQM-0137 | accepted | RBQM-0036, 0043, 0085 accepted |
| DC-RBQM-CORE | kri_breach | 1016 | deviations | RBQM-0135 | accepted | RBQM-0034, 0041, 0083 accepted |
| DC-RBQM-CORE | qtl_breach | STUDY | premature_discontinuation | RBQM-0180 | accepted | RBQM-0038, 0051 accepted |
| DC-RBQM-CORE | site_data_anomaly | 1001 | variance | RBQM-0130 | accepted | RBQM-0039 accepted |
| DC-51448ab6 | kri_breach | 1001 | query_aging | RBQM-0170 | accepted | RBQM-0118 accepted |
| DC-51448ab6 | kri_breach | 1004 | deviations | RBQM-0168 | accepted | RBQM-0116 accepted |
| DC-51448ab6 | kri_breach | 1008 | data_entry_lag | RBQM-0169 | accepted | RBQM-0117 accepted |
| DC-51448ab6 | qtl_breach | STUDY | premature_discontinuation | RBQM-0185 | accepted | RBQM-0122 accepted |
| DC-51448ab6 | site_data_anomaly | 1016 | variance | RBQM-0133 | accepted | RBQM-0081 accepted |

Only 11 findings: 1.0.0 narrated 53 because it took findings of every detector version
(1.0.0 to 2.1.0); 1.1.0 takes only the new versions, which raise 11 on these cuts. The
rejected reply's text is not recorded (by design only accepted text is), only its reason.

### The five known-bad cases, old and new

| Case | 1.0.0 text (the problem) | 1.1.0 counterpart |
| --- | --- | --- |
| RBQM-0001 (DC-BASE, csm, 1001) | "... adjusted p values of 0 or near 0. The flag concerns the last digit of recorded values, with terminal zeros making up about 12%, 11% and 13% ..." | None: csm 2.x raises no terminal-digit finding, and none at 1001 on DC-BASE. |
| RBQM-0006 (DC-BASE, csm, 1009) | "The summary notes that 49% of systolic readings ... end in zero." | None, as above. |
| RBQM-0010 (DC-BASE, csm, 1015) | "About 15% of its systolic readings end in zero, which the finding describes as a terminal-digit pattern." | None, as above. |
| RBQM-0035 (DC-RBQM-CORE, lag, 1004) | "Median data entry lag at Site 1004 is 14.0 days across 325 visits, against a threshold of 7 days. The lag is therefore twice the limit set for this indicator." | RBQM-0136, accepted: "Site 1004: median data entry lag of 14.0 days exceeds the 7-day threshold. The median time to enter data at Site 1004 is 14.0 days, above the 7-day threshold. The 325 visits behind this figure and the ratio to threshold are supporting context only." |
| RBQM-0037 (DC-BASE, QTL approaching) | "The QTL is 61.7%, and the rate is approaching it, having reached 90% of the limit." | None: qtl_detector 2.x uses a fixed approach band (59.7%), so 56.7% on DC-BASE is not approaching. |

Four of the five have no counterpart because the findings they explained no longer
exist, not because 1.1.0 was tried on them. The closest evidence for the framing fix is
RBQM-0130 (csm 2.1.0, 1001 on DC-RBQM-CORE): "The terminal-zero shares (37.3% and 37.6%)
are supporting context only."

### Reads wrong, or reads badly, in 1.1.0

All 10 accepted narratives state their numbers correctly, and every severity matches.
New problems, each a consequence of the stricter prompt:

* **Boilerplate.** All ten label something as "supporting context", most as a closing
  "... is supporting context only". RBQM-0180 calls
  the study-wide scope supporting context ("as is the study-wide scope"), which is wrong.
  RBQM-0136 names "the ratio to threshold" without saying what it is.
* **Jargon from displays.** Rule 4 makes the model copy numbers from displays, and it
  copies the words around them: RBQM-0130 and RBQM-0133 say "robust z of log SD = -5.46
  (threshold -3.5)" to a reader who is not a statistician, where 1.0.0 said "much smaller
  spread". Displays were written for exact numbers, not for plain language.

Candidates for 1.2.0: plain-language labels next to each display, and a prompt that asks
to omit supporting items rather than to label them.

### Tokens and cost

1.1.0, 11 calls: 11,881 input and 1,264 output tokens (about 1,080 and 115 per call; the
described evidence makes the input about 25% longer than 1.0.0's 870). At $0.10 and $0.50
per million tokens: about $0.0018. Step 3 and step 4 together: about $0.0094.

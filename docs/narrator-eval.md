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

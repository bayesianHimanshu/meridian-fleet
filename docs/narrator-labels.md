# Narrator labels (assistant-judged)

Every accepted rbqm.narrator 1.0.0 narrative on DC-BASE, DC-RBQM-CORE and holdout 2
(DC-51448ab6), labelled by the assistant (Claude) after reading it against its
finding's summary and evidence. These labels are a judgment, not ground truth: please
spot-check them. Generated with the labelled set by
`uv run python scripts/build_narrator_labels.py` (data/narrator_eval/labelled.jsonl).

real_bad: 5. real_good: 47. Excluded: 1.

## real_bad

* **RBQM-0001** (expected: zero_p_value). Says 'adjusted p values of 0' (underflow), and presents zero shares near the 10% chance level as what the flag concerns.
* **RBQM-0006** (expected: misattribution). 'The summary notes that 49% ... end in zero': the summary says no such thing; the figures come from the evidence.
* **RBQM-0010** (expected: no specific code). Presents a 15% zero share (near chance) as the terminal-digit pattern. No rule targets this framing; any rejection counts as recall, not as the expected reason.
* **RBQM-0035** (expected: unbacked_ratio). 'The lag is therefore twice the limit': a ratio the finding does not state.
* **RBQM-0037** (expected: threshold_restated). 'having reached 90% of the limit': the rate is 91.9% of it; 90% is where 'approaching' starts.

## Excluded

* **RBQM-0042**. Adds a definition of entry lag that the finding does not state; correct but not from the finding, so neither clearly good nor one of the five.

## real_good

* **RBQM-0002** (DC-BASE). Terminal-digit pattern at 1003 for all three parameters, adjusted p below 0.01, medium severity: all as in the finding.
* **RBQM-0003** (DC-BASE). Pattern at 1004 with p far below 0.01; says the cause is not established, which is fair.
* **RBQM-0004** (DC-BASE). Pattern at 1005; 'the summary notes ... Bonferroni-adjusted over 78 tests' is true of the summary.
* **RBQM-0005** (DC-BASE). Pattern at 1008; zero shares 56%, 49%, 28% match 0.558, 0.486, 0.277 and are well above chance.
* **RBQM-0007** (DC-BASE). Pattern at 1010; 'about 40% of the blood pressure readings end in zero' matches 0.405 and 0.403.
* **RBQM-0008** (DC-BASE). Pattern at 1013; 91.6% zeros for SYSBP and DIABP matches 0.9164.
* **RBQM-0009** (DC-BASE). Pattern at 1014; 'the summary notes ... 78 tests' is true of the summary.
* **RBQM-0011** (DC-BASE). Pattern at 1016; 46% of systolic readings end in zero matches 0.461.
* **RBQM-0012** (DC-BASE). Pattern at 1017; '17% to 29%' spans 0.171 to 0.294.
* **RBQM-0013** (DC-BASE). Pattern at 1018; says the flag is about recorded digits, not clinical: fair.
* **RBQM-0014** (DC-RBQM-CORE). 1001 on DC-RBQM-CORE: digit patterns and, for systolic and pulse, unusual spread, as the summary lists.
* **RBQM-0015** (DC-RBQM-CORE). Pattern at 1003; 'the summary lists terminal digit' is true.
* **RBQM-0016** (DC-RBQM-CORE). Pattern at 1004; 'the summary says the difference is driven by terminal digit patterns' is a fair paraphrase of the listed checks.
* **RBQM-0017** (DC-RBQM-CORE). Pattern at 1005, p far below 0.01, cause not claimed.
* **RBQM-0018** (DC-RBQM-CORE). Pattern at 1008; zero shares 56%, 49%, 28% match the evidence.
* **RBQM-0019** (DC-RBQM-CORE). Pattern at 1009; zero shares 49%, 39%, 20% match the evidence.
* **RBQM-0020** (DC-RBQM-CORE). Pattern at 1010, cause not claimed.
* **RBQM-0021** (DC-RBQM-CORE). Pattern at 1013; zero share about 0.92 matches 0.9164, reported without interpretation.
* **RBQM-0022** (DC-RBQM-CORE). Pattern at 1014; 28% zero share for SYSBP matches 0.279; calls it statistically unusual, not an error.
* **RBQM-0023** (DC-RBQM-CORE). Pattern at 1015; lists zero shares 15%, 13%, 15% neutrally, without calling them the anomaly.
* **RBQM-0024** (DC-RBQM-CORE). Pattern at 1016; 46.1% of systolic readings end in zero matches 0.461.
* **RBQM-0025** (DC-RBQM-CORE). Pattern at 1017; 17%, 28%, 29% match the evidence.
* **RBQM-0026** (DC-RBQM-CORE). Pattern at 1018, 78 tests, medium severity: as in the finding.
* **RBQM-0027** (DC-BASE). 1005 under-reporting: 27 events over 2075 subject-days against 70.2, p and adjusted p as in the summary.
* **RBQM-0028** (DC-BASE). 1015 under-reporting: 15 over 1160 subject-days against 38.6, adjusted p 1.68e-04.
* **RBQM-0029** (DC-BASE). 1016 under-reporting: 86 against about 134, p values as in the summary.
* **RBQM-0030** (DC-RBQM-CORE). 1005 under-reporting on DC-RBQM-CORE: 27 against about 64.3.
* **RBQM-0031** (DC-RBQM-CORE). 1010 under-reporting: 42 against about 130 (129.8), p values as given.
* **RBQM-0032** (DC-RBQM-CORE). 1015 under-reporting: 15 against about 35.4; adjusted p 0.00123 is 1.23e-03.
* **RBQM-0033** (DC-RBQM-CORE). 1016 under-reporting: 86 against about 122, p values as given.
* **RBQM-0034** (DC-RBQM-CORE). 1016 deviations: 18 against about 5.1, adjusted p 9.03e-05.
* **RBQM-0036** (DC-RBQM-CORE). 1008 query aging: median 40.0 days for 43 queries, above 21, high severity.
* **RBQM-0038** (DC-RBQM-CORE). Study-wide QTL breach: 170 of 254 (66.9%) above 61.7%.
* **RBQM-0039** (DC-RBQM-CORE). 1001 variance: smaller spread for SYSBP and PULSE, SD below 0.6 of the median site, digits only supporting.
* **RBQM-0040** (DC-BASE). 1008 deviations: 10 against about 3.0, adjusted p 1.89e-03 over 13 sites.
* **RBQM-0041** (DC-RBQM-CORE). 1016 deviations: 18 against about 5.1, adjusted p 1.57e-06 below 0.01.
* **RBQM-0043** (DC-RBQM-CORE). 1008 query aging: as RBQM-0036.
* **RBQM-0051** (DC-RBQM-CORE). Study-wide QTL breach: 170 of 254 (66.9%) above 61.7%.
* **RBQM-0081** (DC-51448ab6). 1016 variance on holdout 2: less spread for SYSBP and pulse, SD below 0.6, high severity, digits supporting.
* **RBQM-0082** (DC-BASE). 1008 deviations: as RBQM-0040.
* **RBQM-0083** (DC-RBQM-CORE). 1016 deviations: 18 against about 5.1, adjusted p 1.57e-06.
* **RBQM-0084** (DC-RBQM-CORE). 1004 entry lag: median 14.0 days over 325 visits against 7, high severity.
* **RBQM-0085** (DC-RBQM-CORE). 1008 query aging: median 40 days for 43 queries above 21, high severity.
* **RBQM-0116** (DC-51448ab6). 1004 deviations on holdout 2: 17 over 3229 subject-days against about 3.7.
* **RBQM-0117** (DC-51448ab6). 1008 entry lag on holdout 2: median 14.0 days over 325 visits against 7.
* **RBQM-0118** (DC-51448ab6). 1001 query aging on holdout 2: median 40.0 days for 62 queries above 21.
* **RBQM-0122** (DC-51448ab6). Study-wide QTL breach on holdout 2: 162 of 254 (63.8%) above 61.7%.

## Mutations

Each real_good narrative is mutated once per type where the type applies (seed 20261107), and every mutation is labelled bad: wrong_number (one number in the
explanation moved to a nearby value that appears nowhere in the input), flip_direction
(fewer/more, below/above, smaller/larger, lower/higher, little/much), swap_site (the
site id replaced by another study site), unbacked_ratio (a ratio phrase inserted into
the first sentence), supporting_headline (the headline replaced by a supporting
item's display) and zero_p (a p value replaced by 0). The mutations and validator
1.1.0 have the same author, so recall on them is an optimistic measure.

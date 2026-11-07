# AE under-reporting experiment (phase 1 step 5)

## Why

In step 4, rbqm.kri_monitor 2.0.0 missed AE under-reporting everywhere, even a 70% drop.
Pilot AE rates per subject-day vary about 7x between sites, so the dispersion phi on
DC-BASE is about 8 and a real drop looks like ordinary site-to-site variation. This
experiment tries five ways to compute the expected AE count of a site, chosen in advance,
and picks one by a rule fixed before any number was computed.

## Pre-registration

Written and committed before the experiment was run.

Variants (`meridian/stats/ae_expectation.py`). Each gives, per tested site, the observed
count O_i and an expected count E_i that does not use the site's own data. Treated
subjects only; the blinded treatment arm is never used. The quasi-Poisson test is the
step 4 one, unchanged: phi from residuals against the median site rate (for variant d,
per unit of model-expected count), z = (O - E) / sqrt(phi E), one-sided lower p,
Bonferroni over the tested sites, alpha 0.01.

| id | name | O_i | E_i |
| --- | --- | --- | --- |
| a | subject_days (current) | AEs at the site | other sites' AEs per subject-day x site subject-days |
| b | per_subject | AEs at the site | other sites' AEs per treated subject x site treated subjects |
| c | early_window | AEs with onset in treatment days 1 to W (W = 42) | other sites' early AEs per capped subject-day x site subject-days capped at W per subject |
| d | covariate_adjusted | AEs at the site | sum over the site's subjects of a Poisson regression fitted on the other sites: log E = b0 + age group (<70, 70-79, >=80) + sex + log(subject-days) offset |
| e | lab_concordance | AEs at the site | other sites' AEs per abnormal lab result (LBNRIND HIGH, LOW or ABNORMAL) x site abnormal lab results |

Dev data: DC-BASE, DC-NULL-S1 to S5, DC-RBQM-CORE and the three AE sweep cuts
(DC-SWEEP-AE-UNDER-REPORTING-0P2, -0P4, -0P7). The step 4 holdout and the other sweep cuts
are not used.

For each variant, report: phi on DC-BASE; detector false positives on DC-BASE and on each
null cut; whether the AE fault is detected on DC-RBQM-CORE and on each AE sweep level.

Selection rule:

1. Eligible: 0 AE false positives on DC-BASE and on every null cut at alpha 0.01.
2. Pick the eligible variant that detects the most AE faults on dev (DC-RBQM-CORE and the
   three AE sweep cuts, 4 at most).
3. Ties go to the lower phi on DC-BASE, then to the simpler variant in the order
   a, b, c, e, d.
4. If no variant detects more AE faults than a, keep a and record AE under-reporting as a
   known limitation. No variant f is invented.

The chosen variant, with the dev results, is committed with the config as
"Freeze AE expectation variant before holdout 2". Only after that commit is holdout 2
generated. Its results are reported once and nothing is retuned after seeing them.

Note, known before running: the null cuts share their AE, DM, SV and LB data with
DC-BASE (only the operational tables differ), so a variant's AE false positives are the
same on DC-BASE and on every null cut. The null columns are still reported.

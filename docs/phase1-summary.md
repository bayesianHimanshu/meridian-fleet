# Phase 1 summary

Phase 1 built a fleet of three RBQM agents for the fictional MRD-201 study and a way to
measure them honestly. This page is the close-out. The numbers come from
[eval-phase1.md](eval-phase1.md), which `meridian eval report` regenerates.

## What was built

| Step | What |
| --- | --- |
| 1 | Domain types, an append-only SQLite ledger (STRICT table, triggers, SHA-256 hash chain, verifier), the injected Clock, settings and the CLI. |
| 2 | The CDISC pilot SDTM becomes MRD-201 with a blinded / ops / unblinded split and seeded synthetic operational data (entry lag, queries, deviations). Agents can only read blinded and ops tables. |
| 3 | A fault catalog (six faults, target-level parameters, no trace in the cut, ground truth kept apart), an agent runner with dedup keys, and v1 of rbqm.kri_monitor, rbqm.qtl_detector and rbqm.csm. |
| 4 | Agent version as part of identity; v2 statistics (quasi-Poisson KRIs with a robust phi, a QTL approach band, a between-site robust z for vital-sign variance); the eval harness (matching, metrics, append-only scorecards); five null cuts, a holdout and an 18-cut magnitude sweep, after a threshold freeze. |
| 5 | Rules scored against condition truth and detectors against planted faults; a pre-registered experiment with five expected-AE variants (with a hand-written IRLS Poisson regression); opaque cut ids; a fresh holdout 2 and three AE holdouts, after a second freeze. |

## v1, v2 and v2.1

| | DC-BASE false positives (all findings) | DC-RBQM-CORE recall (all faults) | DC-RBQM-CORE detector recall |
| --- | ---: | ---: | ---: |
| v1 (step 3, untuned) | 17 | 1.00 | 1.00 |
| v2 (step 4) | 1 | 0.83 | 0.67 |
| v2.1 (step 5, kri_monitor 2.1.0) | 1 | 0.83 | 0.67 |

v1 found everything because it flagged almost everything: 13 of its 17 clean-cut false
positives were CSM variance tests that a real between-site difference in measurement
habits trips at 1,700 readings per site. v2 removed 16 of 17 at the cost of AE
under-reporting. v2.1 is v2 with the AE expectation made configurable; the experiment
kept variant a, so v2.1 behaves exactly like v2 and is a distinct version only so its
runs are separate in the ledger.

## Exit criteria, original and revised

The criteria were revised after seeing the step 4 results: step 4 counted a rule finding
at a site that really breaches its threshold (a synthetic median entry lag of 7.4 days
against 7) as a false positive. That was an error in the measurement, not the agent.

| Original criterion (step 4) | v2 | v2.1 |
| --- | --- | --- |
| Recall >= 0.8 on DC-RBQM-CORE | 0.83 pass | 0.83 pass |
| Recall >= 0.8 on DC-RBQM-HOLDOUT | 0.83 pass | 0.83 pass |
| Recall >= 0.8 on holdout 2 (DC-51448ab6) | n/a | 0.83 pass |
| False positives on DC-BASE <= 1 | 1 pass | 1 pass |
| Mean false positives per null cut <= 1 | 1.40 FAIL | 1.40 FAIL |

| Revised criterion (step 5) | v2 | v2.1 |
| --- | --- | --- |
| Detector recall >= 0.8 on DC-RBQM-CORE | 0.67 FAIL | 0.67 FAIL |
| Detector recall >= 0.8 on the holdout (v2: DC-RBQM-HOLDOUT, v2.1: holdout 2) | 0.67 FAIL | 0.67 FAIL |
| Detector false positives on DC-BASE <= 1 | 1 pass | 1 pass |
| Mean detector false positives per null cut <= 1 | 0.40 pass | 0.40 pass |
| Rules agree 100% with condition truth on every cut | 26 of 26 cuts | 30 of 30 cuts |
| Ledger verifies | | pass |

The revision fixes the null-cut failure, which was a measurement error, and exposes the
real one: of three detector faults per dev or holdout cut, AE under-reporting is never
found, so detector recall is 2 of 3. Phase 1 does not meet its revised detector recall
criterion, and this page does not pretend otherwise.

## Detection limits (magnitude sweep, one fault per cut)

| Fault | Check | Smallest level caught | Not caught |
| --- | --- | --- | --- |
| fabricated_vitals | variance (detector) | SD scaled to 0.25 | 0.5, 0.75 |
| ae_under_reporting | ae_rate (detector) | none | 20%, 40%, 70% drop |
| deviation_cluster | deviations (detector) | 4x the other sites' rate | 1.5x, 2.5x |
| entry_lag_spike | data_entry_lag (rule) | median 9 days | 6 days is below the 7-day threshold by design |
| query_backlog | query_aging (rule) | median 25 days | 18 days is below the 21-day threshold by design |
| discontinuation_drift | premature_discontinuation (rule) | base + 0.03 (approaching) | none |

Rules catch exactly what crosses their threshold, by construction. The detectors need
large effects: only a strong shrinkage of vital-sign spread and a fourfold deviation rate
stand out from the natural differences between pilot sites.

## Known limitations

* **AE under-reporting is undetectable on this data.** AEs per 100 subject-days range
  from about 1 to 5.5 between tested sites, so phi is about 8 on DC-BASE. A 70% drop at
  1010 gives z = -1.9 against a Bonferroni cut-off of about -3.2, and no expected-AE
  variant changed that: per subject, an early window, age and sex adjustment and abnormal
  labs all left the target between z = -1.6 and -1.9 on dev. On holdout 2 and the three AE
  holdouts the target z was between -1.4 and -2.6: all missed. Most of the between-site
  spread probably comes from what is blinded (treatment arm, since xanomeline causes skin
  reactions) and from site reporting culture, neither of which a blinded agent may use.
* **The fault inflates the dispersion that hides it.** 1010 sits just above the median
  site rate; removing its AEs moves the median and doubles phi (8.3 to 16.6), so the drop
  makes every site look noisier. A reference that does not move with one site (a
  trimmed or prior-anchored rate) would help; it was not in the pre-registered set.
* **Small sites are not tested.** Sites with fewer than 5 treated subjects are skipped;
  no holdout target had fewer than 10.
* **One natural deviation outlier.** Site 1008's synthetic deviation rate (z = 3.6) is a
  detector false positive on DC-BASE and on every cut that shares its ops tables.
* **Cut ids before step 5 name their faults** (for example DC-SWEEP-ENTRY-LAG-SPIKE-14).
  No agent reads cut ids, and from step 5 on cut ids are opaque.
* **Phase 1 data are one real study** with synthetic operational tables; the detector
  limits above are limits on this study, not general ones.

## Five lessons

1. **Measure the measurement first.** The step 4 null-cut failure was the eval scoring a
   correct rule finding as wrong. Before tuning an agent, check that the scoring rule
   matches what the check claims to do: a threshold rule is right whenever the threshold
   is crossed, planted or not.
2. **Freeze, then look.** Thresholds were frozen before the holdout and the AE variant
   before holdout 2, each in a commit the report names, and the scorecards record the
   commit they were written at. The holdout numbers are therefore honest, including the
   bad ones.
3. **Pre-register, and accept a null result.** The AE experiment fixed its variants, data
   and selection rule in a commit before running. None beat the baseline, so the
   baseline stayed. Without the rule it would have been tempting to keep adding variants
   until one worked on dev.
4. **Real sites are not interchangeable.** Tests that pool other sites and assume pure
   chance flag almost everything at real sample sizes. Robust between-site statistics
   (median and MAD, a robust dispersion) are what made the false positives fall from 17
   to 1, and they also set the floor on what can be detected.
5. **Determinism is a feature you test for.** An unsorted polars group_by once made a
   cut non-reproducible. A contract test now checks every group_by, cuts and scenarios
   are written once, and opaque cut ids are a hash of the scenario bytes, so the same
   inputs always give the same cut.

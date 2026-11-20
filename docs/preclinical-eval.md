# preclinical.tox_study_monitor: pre-registration and evaluation

Phase 5 step 1. **Everything above "Results" was committed before any agent code existed
and before the agent ran on any data** (commit of this file; `git log --follow`). The
thresholds are in `config/default.toml` `[agents.tox_study_monitor]`; changing one is a
new agent version. Background: docs/preclinical-notes.md.

## What the agent does and does not say

- It compares each dose group with the **concurrent control** of the same sex and phase,
  never with historical control data (1.0.0).
- It may say a change is **dose-related** (a significant trend and a flagged dose group)
  or a **change without a trend**. It never says "adverse", never proposes a NOAEL or a
  LOAEL: those are the toxicologist's decisions (NoaelDecision, recorded through the API).
- A record it cannot use becomes a **data-quality finding**; the run never fails on data.

## Analysis population

- **Groups** come from TX. A set is **toxicokinetic** if TKDESC is "TK" (excluded from every
  analysis); **recovery** if TX has RECOVDUR, else if its SET name contains the word
  "recovery" not preceded by "non"; the **control** is the set with TCNTRL present and dose
  0 (if a phase has more than one, the one whose TCNTRL contains "vehicle"; if still
  ambiguous, a data-quality finding and no analysis of that phase). Dose groups are ranked
  by TRTDOS within each phase: control 0, then 1, 2, 3.
- **Phases** are analysed separately: **main** (sets that are neither TK nor recovery) and
  **recovery**. Sexes are analysed separately (DM SEX).
- **Deaths:** DS DSDECOD other than TERMINAL SACRIFICE and RECOVERY SACRIFICE is an
  unscheduled death.

## Endpoints

| Endpoint | Value per animal | Class (magnitude) |
|---|---|---|
| Body weight (BW, BWTESTCD BW) | the weight at the phase's final weighing day: the most common last BW day among the phase's animals; animals without a weight that day (early deaths) are left out | body_weight (5%) |
| Body weight gain | that weight minus the animal's first weight (its earliest BWDY) | body_weight_gain (10%) |
| Clinical pathology (LB, each LBTESTCD with LBSTRESN) | the value at the phase's scheduled collection day for that test: the most common LBDY among the phase's animals; other days are unscheduled and left out | LBCAT: HEMATOLOGY 10%, COAGULATION 10%, CLINICAL CHEMISTRY 20%, URINALYSIS 25%, else 20% |
| Organ weight, absolute (OM, OMTESTCD WEIGHT, each OMSPEC) | OMSTRESN | organ_weight (10%) |
| Organ weight relative to body weight | OMSTRESN / terminal body weight (BW TERMBW) x 100 | organ_weight (10%) |
| Microscopic findings (MI, each MISPEC and MISTRESC other than NORMAL, UNREMARKABLE or empty) | affected if any record; examined if the animal has an MI record for that specimen (MISTAT not NOT DONE) | incidence |
| Macroscopic findings (MA, each MASPEC and MASTRESC other than NORMAL, UNREMARKABLE, NO VISIBLE LESIONS or empty) | affected if any record; every animal of the group is examined at necropsy | incidence |
| Clinical observations (CL, each CLCAT and CLSTRESC other than NORMAL or empty) | affected if observed on any day | incidence |
| Mortality | an unscheduled death (above) | incidence |

MI severity (MISEV: MINIMAL 1, MILD 2, MODERATE 3, MARKED 4, SEVERE 5) is reported in the
evidence per group (count per grade and mean grade of the affected animals); 1.0.0 does
not test it.

**Unusable records (data-quality findings, one per kind of problem per domain):** an LB
value whose LBSTRESU differs from the unit most of that test's values have in the study
(no conversion in 1.0.0); a numeric value with an empty unit when the test has a unit;
two values for one animal, test and day; a dose group or sex not in TX or DM; a control
with fewer than `min_per_group` (3) usable values for an endpoint (that endpoint is not
analysed in that sex and phase). NOT DONE records and qualitative results are not used and
are not reported.

## Methods (pre-registered)

**Continuous endpoints**, per endpoint, sex and phase, groups with at least 3 values:
- Group mean, SD, N, and percent difference of each dose group's mean from the control's.
- **Dunnett's test** (many-to-one against control, two-sided; `scipy.stats.dunnett`, its
  numerical integration seeded with `dunnett_seed`).
- **Jonckheere-Terpstra trend test** across control < low < mid < high (two-sided; normal
  approximation with the tie correction; Hollander and Wolfe, Nonparametric Statistical
  Methods, 3rd ed., section 6.2).
- A dose group is **flagged** when Dunnett p < 0.05 **and** |percent difference| is at least
  the class magnitude **and**, with the group's single animal farthest from the control
  mean left out, |percent difference| is still at least the magnitude (so one animal never
  makes a finding).
- **dose_related_change:** at least one flagged group, trend p < 0.01, and every flagged
  group and the trend in the same direction.
- **pairwise_change:** otherwise, a flagged group with Dunnett p < 0.01 (a change without a
  trend, for example only at the mid dose).

**Incidence endpoints**, per endpoint, sex and phase:
- Incidence (affected / examined) per group; MI severity as above.
- **Cochran-Armitage trend test** (one-sided, increase; scores = dose rank 0, 1, 2, 3;
  Agresti, Categorical Data Analysis, 2nd ed., section 5.3.5) and **Fisher's exact test**
  of each dose group against control (one-sided, increase).
- **dose_related_incidence:** trend p < 0.05 and the top dose group exceeds the control by
  at least 2 animals and at least 20 percentage points.
- **pairwise_incidence:** otherwise, a dose group with Fisher p < 0.01 that exceeds the
  control by at least 2 animals and 20 points.
- Decreases in incidence are not raised in 1.0.0.

No adjustment for the number of endpoints: each endpoint is judged on its own, as a study
report does, and the null evaluation below measures what that costs.

**Severity of a finding** (its triage weight, not adversity): dose_related_* medium,
pairwise_* low, data quality low. Each finding carries the group table (N, mean or
incidence, percent difference, p-values) as evidence, with roles and display strings.

## Evaluation (targets stated before running)

All evaluation copies are made from the curated study (MRD-101-TOX-01, TOX-FINAL), run in
process through the same agent code (`meridian preclinical eval`), and never recorded in
the ledger: no evaluation finding can reach a reviewer's queue.

**Null cuts.** 20 copies (seeds 1 to 20), each with the group labels (SETCD, ARMCD) of the
main-phase animals permuted among the main-phase animals of the same sex, and the same
within the recovery phase; TK animals untouched. No true effect remains. Data-quality
findings (which depend on the records, not the groups) are reported apart.
- **Target:** at most 6 effect findings per null cut on average, at most 3 of them
  dose_related_*, and no null cut with more than 12.

**Planted effects.** Copies of a null cut (seeded permutation as above, then liver
hypertrophy records reset to NORMAL), with, in the main phase of both sexes:
- ALT and AST x 1.5 at the mid dose and x 2.5 at the high dose;
- body weight gain at the high dose reduced by 30% (the final-day weight lowered so that
  gain falls by 30%; the lower final weight may also be flagged and is not a false
  positive);
- liver weight x 1.10 at the mid dose and x 1.25 at the high dose (absolute; the relative
  weight follows);
- liver hepatocellular hypertrophy (MI LIVER, HYPERTROPHY, MINIMAL or MILD) in 0, 0, 3 and
  8 animals of 10 per group;
- one high-dose male's creatinine (CREAT) set to 5 times the median of its group: a
  single-animal outlier that **must not** become a finding.

Ground truth per copy in the format of data/ground_truth (GroundTruth, one FaultRecord
per effect and sex). Sets: **dev** 10 copies (seeds 101 to 110), **holdout** 10 copies
(seeds 201 to 210); holdout is run once, after dev, with nothing changed between.
- **Targets:** each planted effect found (the right endpoint, sex, phase and direction, as
  dose_related_* or pairwise_*) in at least 90% of copies of each set, ALT and AST with
  both the mid and high dose flagged in at least 80%; the outlier raised in 0 copies;
  findings on endpoints nothing was planted in at most the null target.

**Stress set.** 13 hand-written cases in tests/fixtures/tox_stress (each a study spec and
its ideal findings), committed with this file, before the agent code: effect in one sex
only, effect only at the mid dose, recovery groups present (effect reversing and
persisting), an animal that died early, deaths rising with dose, missing control values, a
unit change within LB, a single-animal outlier, a small but significant change, liver
hypertrophy incidence, a dose-related change in both sexes, and clean controls. Run once,
at the end (`scripts/tox_stress.py`). It tests the specification above, written by
the same author as the agent: agreement shows the code does what the rules say, not that
the rules are what a toxicologist would decide.

**The real study.** The agent runs once on MRD-101-TOX-01 in a Substrate sandbox on kind;
every finding is listed next to the signals its reviewer's guide says were built in
(docs/preclinical-data.md).

## Results

Run on 15 Nov 2026, preclinical.tox_study_monitor 1.0.0, nothing above changed. Scorecards:
`data/evals/preclinical/{null,dev,holdout}.json` (git-ignored); ground truth:
`data/ground_truth/preclinical/PRE-*.json`. Re-running each set gives the same files byte
for byte.

**A process slip, said plainly.** While writing the agent, I ran the stress specs once as a
smoke test (before the formal run below), which the protocol says not to do. It showed the
same two differences as the formal run. No rule or threshold was changed in response;
the formal run is the one reported.

### Null cuts (20 permutations): targets missed

| | Target | Result |
|---|---:|---:|
| Effect findings per null cut, mean | at most 6 | **9.2** (2 to 15) |
| dose_related_* per null cut, mean | at most 3 | **7.7** |
| Null cuts with more than 12 | none | **5 of 20** (13, 13, 13, 15, 15) |

Of the 184 effect findings over the 20 cuts, **134 are dose_related_incidence** on rare
lesions (MI 84, MA 35, CL 15 across all checks). Cause: two affected animals out of 40
that land, by chance, both in the top dose group give a Cochran-Armitage z of 1.95
(p = 0.026, normal approximation) and meet the 2-animal, 20-point excess; the exact chance
of that arrangement is about 0.058. The asymptotic trend test is anti-conservative for
sparse counts, and there are many rare lesions. Continuous endpoints contribute 2.4 a
cut. Data-quality findings (8 to 12 a cut) are reported apart.

### Planted effects (dev 10 copies, holdout 10 copies, once each)

| Effect (main study) | Target | Dev | Holdout |
|---|---:|---:|---:|
| ALT up, males / females | 90% | 100% / 100% | 90% / 100% |
| AST up, males / females | 90% | 100% / 100% | 100% / 100% |
| ALT and AST with mid and high flagged (M ALT, M AST, F ALT, F AST) | 80% | 50, 60, 100, 70% | 60, 70, 100, 70% |
| Body weight gain down, males / females | 90% | **20% / 10%** | **0% / 10%** |
| Liver weight up, males / females | 90% | 90% / **10%** | 90% / **0%** |
| Hepatocellular hypertrophy, males / females | 90% | 100% / 100% | 100% / 100% |
| Single-animal creatinine outlier raised | 0 copies | **0** | **0** |
| Findings on unplanted endpoints, mean per copy | at most 6 | 7.8 (max 14) | 8.0 (max 22) |

Met: ALT, AST, hypertrophy, male liver weight, the outlier. Missed: body weight gain,
female liver weight, mid and high flagged together, false positives. Why the misses, from
the ground truth's achieved values (`achieved` in each FaultRecord, dev): high-dose gain
changed by -10% to -39% in males and +6% to -56% in females, female liver weight by -6% to
+66%, because a permuted copy of this study carries the study's own strong effects as
noise (in some copies the real high-dose animals land in the control group). In permuted copies the CV of body
weight gain is 20% to 61% and of female liver weight 17% to 43% per group, against 28%
and 14% in the real control groups, so a 25% to 30% change at 10 animals often misses
Dunnett's 5%. The evaluation design (planting on a permutation of a study with large real
effects) is the main cause, not a fault in the planting.

### Stress set (13 cases, run once): 11 as ideal

`scripts/tox_stress.py`, actuals written into each fixture under `1.0.0`.

| Case | Ideal | 1.0.0 |
|---|---|---|
| clean_controls | nothing | as ideal |
| dose_related_both_sexes | ALT dose-related, M and F | as ideal |
| one_sex_only | AST, F only | as ideal |
| mid_dose_only | change without a trend, M and F | as ideal |
| recovery_reverses | ALT main only | **extra:** relative kidney weight, F, recovery (chance, n = 5) |
| recovery_persists | liver weight abs and rel, both phases and sexes | as ideal |
| early_death | nothing | as ideal |
| deaths_rise_with_dose | mortality dose-related, M and F | as ideal |
| missing_control_values | data quality: too few control ALT, M | as ideal |
| unit_change_in_lb | data quality: GLUC unit | **extra:** body weight, M (chance, no trend) |
| single_animal_outlier | nothing | as ideal |
| small_but_significant | nothing | as ideal |
| hypertrophy_incidence | dose-related incidence, M and F | as ideal |

It tests the specification: the cases and the agent have one author.

### The real study, in a Substrate sandbox on kind

One run on MRD-101-TOX-01 (SendStudyLoaded seq 19601), template
`preclinical-39e5e8f82643-m384`, gVisor, 6.3 s, 0 failed; 133 findings (PRE-0001 to
PRE-0133, seq 19602 to 19735), the drafts identical to an in-process run (drafts sha256
`c6c93308...`). By rule: dose_related_change 78 (main 46, recovery 32), dose_related_incidence
35 (26, 9), pairwise_change 11 (5, 6), data quality 9 (too few control values: GGT and
urine protein, which are mostly non-numeric, and two recovery lung findings). 54 of the
effect findings are organ weights, 27 in each phase: at 200 mg/kg body weight is 18% to 19%
lower, so organ weights relative to body weight rise across organs.

Next to the reviewer's guide's built-in signals (docs/preclinical-data.md), main study:

| Reviewer's guide | The agent (main study) |
|---|---|
| Body weight and gain lower in groups 3 and 4 | Group 4 only: body weight -18% M, -19% F; gain -49% M, -58% F, dose-related. Group 3: -2% / +2%, not flagged |
| AST, ALT, ALP higher in group 4 | AST dose-related, M at 200 (+41%), F at 20 and 200 (+30%, +56%); ALP dose-related M, F at 200 (+28%, +52%); ALT dose-related F at 200 (+25%), change without a trend M at 200 (+34%, trend p 0.034) |
| RBC, HGB, HCT lower in group 4 | **Not raised.** Significant trends (HCT and HGB p < 0.03) but -3% to -9%, below the 10% haematology magnitude |
| Liver weights higher in group 4 | Females: absolute +90%, relative +131%, dose-related. Males: absolute -1% (no change in the data); relative +21% flagged at 200 but trend p 0.011 and Dunnett p above 0.01, **not raised** |
| Liver findings in groups 3 and 4 | Hypertrophy at 200: M 6/10, F 9/10; enlarged liver (MA) M 4/10, F 5/10; vacuolization M; necrosis F at 20 and 200 (4/10, 2/10), all dose-related. **Not raised:** necrosis M (0, 0, 4, 1; trend p 0.067), inflammation at 20 (2/10) |
| Liver tumours in group 4 | Hepatocellular adenoma M 2/10, dose-related. **Not raised:** carcinoma M 1/10 |
| Premature deaths (carcinoma) in group 4 | **Not raised:** 1 male at 200 and 1 in control (one death each) |

### What this means for the next version (not applied: 1.0.0 stays as registered)

- **Sparse incidences:** an exact (permutation) Cochran-Armitage test, or at least 3
  affected animals for dose_related_incidence, would cut most null findings; to be
  pre-registered and evaluated on fresh seeds before use.
- **Haematology magnitude:** 10% missed the guide's red-cell decrease (-4% to -9% with
  clear trends); a toxicologist reads such a coherent pattern as treatment-related.
- **Evaluation design:** plant on a permutation with the study's real effects removed (or
  on a synthetic base), so the planted-effect targets measure the agent, not the noise.

### Decisions the specification did not make

- A data-quality finding per kind of problem **and endpoint** (the stress ideals' form),
  not per domain as the text above says.
- An LB test code measured in more than one specimen (PROT in serum and urine) is one
  endpoint per specimen (`lb:PROT:URINE`).
- Incidence denominators for MA and CL are all animals of the group; for MI, the animals
  with any record for that specimen.

## 1.1.0: changes and targets (pre-registered)

Phase 5 step 1b. **This section, the 1.1.0 stress ideals and the synth additions they
need were committed before any 1.1.0 code** (`git log --follow` on this file). Nothing in
the 1.0.0 sections above changes; 1.0.0's results stand as reported. Holdout is run once,
at the end; null and dev may be run while building to catch bugs, and any change after
such a run is listed under 1.1.0 results with its reason (a bug fix, never a threshold).

### A. The evaluation base: control animals only

The 1.0.0 copies were permutations of the real study, so its real 18% to 90% effects
became noise in every group. 1.1.0 is evaluated on a **control base**:
- For each phase (main, recovery) and sex, the pool is that phase's concurrent control
  animals of that sex in MRD-101-TOX-01 (main 10 M, 10 F; recovery 5 M, 5 F).
- Every analysed animal of every group (control included) gets a source animal drawn
  uniformly **with replacement** from its pool (numpy `default_rng(seed)`, in USUBJID
  order). It keeps its own id, sex and group (DM); its records in BW, LB, OM, MI, MA, CL,
  DS and DD are replaced by the source animal's, relabelled with its id. TK animals and the
  other domains are untouched.
- So the base has no treatment effect and the controls' own variance (10 distinct animals
  per sex in main, 5 in recovery; resampling repeats some, which the null cuts measure).
- **Null cuts:** 20 bases, seeds 1001 to 1020, as they are. **Planted sets:** a base plus
  the effects below, dev seeds 1101 to 1110, holdout seeds 1201 to 1210. The 1.0.0 sets
  (seeds 1 to 20, 101 to 110, 201 to 210) are retired.

**Planted effects** (main phase, both sexes), the 1.0.0 effects unchanged plus three:
- ALT and AST x 1.5 at the mid dose and x 2.5 at the high dose;
- body weight gain at the high dose reduced by 30% (final weight lowered, as in 1.0.0);
- liver weight (absolute) x 1.10 at the mid dose and x 1.25 at the high dose;
- liver hepatocellular hypertrophy (MINIMAL or MILD) in 0, 0, 3 and 8 of 10 animals;
- one high-dose male's CREAT at 5 x its group median (must not be raised);
- **new:** RBC, HGB and HCT x 0.94 at the high dose (-6%, inside the guide's 3% to 9%);
- **new:** one high-dose animal per sex dies unscheduled (DS MORIBUND SACRIFICE on day
  60; its BW after day 60, its LB and OM records removed), with an MI record LIVER NECROSIS,
  MARKED, MIDTHREL Y (the cause);
- **new:** one mid-dose animal per sex with MI LIVER HEPATOCELLULAR CARCINOMA, MIRESCAT
  MALIGNANT (one animal, no other neoplasm planted).

Consequences allowed (not required, not false positives): body weight at the high dose,
relative liver weight, and the grouped "may be secondary to lower body weight" finding at
the high dose (the 30% gain loss lowers body weight by about 10% to 15%).

### B. Statistics for small counts and red cells

- **Incidence trend: exact Cochran-Armitage.** The trend p is the exact conditional
  (permutation) p: given the group sizes and the total affected, P(T >= T observed) for
  T = sum of dose rank x affected, computed exactly by convolving the hypergeometric
  counts of each group (no simulation). alpha 0.05, one-sided, as in 1.0.0; the 2-animal,
  20-point excess and Fisher's pairwise rule unchanged. Why: 134 of the 184 null findings
  in 1.0.0 were dose-related incidences, mostly 2 of 40 animals both in the top group,
  approximate p 0.026 against an exact 45/780 = 0.058. Under the exact test that pattern
  is never raised; 3 of 3 in the top group (exact 0.012) still is. Chosen over a
  3-animal minimum because the exact test keeps the 0.05 level for any count, where a
  fixed minimum is arbitrary for larger groups.
- **Red cell mass (RBC, HGB, HCT): 3% magnitude, with coherence.** A group of one of these
  is flagged at a change of at least **3%** (instead of the 10% haematology magnitude)
  when, at the same dose, sex and phase, **at least one other of the three** is also
  flagged at 3% in the same direction; otherwise the 10% magnitude applies. Dunnett,
  leave-the-extreme-out, and the trend rule are unchanged. Source of the convention:
  there is no published fixed percent; interpretation guidance for haematology in
  toxicity studies (N. Everds, "Hematology interpretation for toxicity studies", HSTalks
  1349, and her "Mechanisms and investigations into hematotoxicity" slides) asks for
  comparison with concurrent controls, magnitude, time course and a coherent pattern
  across the red cell mass parameters, which move together; red cell mass parameters
  vary little between control rats (CV 1.0% to 3.5% in this study's main controls,
  against 10% to 12% for ALT and AST), so 3% is about one control SD; and the study's own
  reviewer's guide calls 3% to 9% decreases treatment related. The coherence condition
  is what keeps 3% from flagging a single noisy parameter. **Cost:** measured on the null
  cuts as the red-cell findings under this rule minus those with a plain 10% magnitude.

### C. Rules applied regardless of statistics, and consolidation

Each is its own finding kind (check):
- **`unscheduled_death:<USUBJID>`**: every animal of a treated, non-TK group whose DS
  disposition is not a scheduled sacrifice. One finding per animal: sex, dose, phase, day,
  disposition, and the **cause**: the animal's MI records with MIDTHREL Y (specimen and
  finding); if none, DD's DDSTRESC; if neither, "cause not recorded". Severity high.
  Control deaths are not raised (they stay in the mortality incidence endpoint).
- **`neoplasm:mi:<MISPEC>:<MISTRESC>:<sex>`**: any MI finding that is a neoplasm (MIRESCAT
  BENIGN or MALIGNANT; where MIRESCAT is empty, a term naming a neoplasm: ADENOMA,
  CARCINOMA, SARCOMA, LYMPHOMA, LEUKEMIA, PAPILLOMA, FIBROMA, LIPOMA, LEIOMYOMA, THYMOMA,
  MESOTHELIOMA, PHEOCHROMOCYTOMA, NEOPLASM, TUMOR) in at least one animal of a treated
  group, in either phase. One finding per neoplasm and sex, every group of both phases as
  evidence, with the exact trend p. A neoplasm endpoint is never raised as a
  dose_related_incidence or pairwise_incidence too. Severity medium.
- **`organ_weight_secondary_to_bw:<sex>:<phase>:r<rank>`**: when a dose group's mean body
  weight (bw:BW, final weighing) is at least **10% below control**, every relative organ
  weight flagged at that dose whose absolute weight of the same organ is not flagged at
  that dose in the same direction goes into this ONE finding per sex, phase and dose,
  worded "may be secondary to lower body weight". A relative weight whose every flagged
  dose is so explained has no finding of its own; one also flagged at a dose without the
  body weight drop keeps its own finding. Severity low.
- **Recovery as evidence.** For each main-study finding (an endpoint and sex), the
  recovery groups give its **recovery status**, judged at the highest flagged main dose
  against the recovery group of the same dose:
  continuous, with m and r the main and recovery percent differences and M the magnitude
  used: **reverses** if r is in the other direction or |r| < M; **persists** if
  |r| >= 0.5 |m|; else **partly reverses**. Incidence, with e_m and e_r the excess over
  control in percentage points: **reverses** if e_r <= 0; **persists** if e_r >= 0.5 e_m;
  else **partly reverses**. No recovery group at that dose, or too few values: **not
  assessed**. The status and the recovery group table are evidence on the main finding.
  A change that meets a rule only at recovery (its endpoint and sex have no main finding)
  is raised as **`recovery_only_change`** or **`recovery_only_incidence`**
  (`:<endpoint>:<sex>:recovery`).
- **Consolidation:** one finding per endpoint and sex (`<check>:<endpoint>:<sex>:main`),
  all dose groups as evidence, recovery folded in as above. (1.0.0 already gave one
  finding per endpoint, sex and phase; 1.1.0 removes the separate recovery finding.)
  Absolute and relative organ weights stay separate endpoints.
- Unchanged: Dunnett, Jonckheere-Terpstra, magnitudes other than red cells, the robustness
  rule, mortality as an incidence endpoint, data-quality findings, the wording rules (never
  "adverse", never a NOAEL).

1.0.0's findings are superseded (same check) or retired (a 1.1.0 run on TOX-FINAL completed
without raising that check) by the existing rules.

### D. Targets (stated before running)

"Statistical findings" are dose_related_*, pairwise_*, recovery_only_* and
organ_weight_secondary_to_bw; the rule findings (unscheduled_death, neoplasm) and data
quality are counted apart.

**Null cuts (20 control bases):**
- statistical findings per cut, mean **at most 5**; dose_related_* per cut, mean **at most
  2**; no cut with more than **10**;
- rule findings: exactly the resampled control deaths that land in treated groups (the
  main male control pool holds one unscheduled death, a gavage error, so about 3 per cut
  are expected and correct); no neoplasm (the control pools hold none);
- red-cell cost: at most **0.5** added red-cell findings per cut on average (1.1.0 rule
  against a plain 10%).

**Planted sets, per sex, each set** (power from the control CVs above, n = 10, Dunnett at
5% across three doses):

| Planted effect | Target, M | Target, F |
|---|---:|---:|
| ALT up, AST up (each) | 95% | 95% |
| ALT and AST with mid and high both flagged | 90% | 90% |
| RBC, HGB, HCT down 6% (each) | 80% | 80% |
| Body weight gain down 30% (control CV 19% M, 28% F) | 80% | 50% |
| Liver weight up 25% (control CV 22% M, 14% F) | 50% | 90% |
| Hepatocellular hypertrophy | 95% | 95% |
| Planted death raised, with its MI cause | 100% | 100% |
| Single-animal carcinoma raised as a neoplasm | 100% | 100% |
| CREAT outlier raised | 0 copies | |
| Statistical findings on unplanted endpoints, mean per copy | at most 5 | |

**The real study (one run in a Substrate sandbox on kind):**
- **50 to 90 findings** in total, data quality included (1.0.0: 133);
- every signal of the reviewer's guide covered: body weight and gain lower at 200 mg/kg;
  AST, ALT and ALP higher; **RBC, HGB and HCT lower** (at least one red-cell finding per
  sex); liver weight higher (females by its own finding; males through the absolute or the
  relative weight, own or grouped finding); liver findings (hypertrophy, enlargement,
  vacuolization, necrosis); **the liver adenoma and the liver carcinoma** as neoplasm
  findings; **the group 4 death**, with its MI cause. Expected not covered, said now:
  group 3 body weight (-2% / +2% in the data), and male liver necrosis at 20 mg/kg
  (0, 0, 4, 1 of 10: no trend, Fisher p 0.043 above 0.01).
- The real study's values were seen in 1.0.0's report, so the red-cell and death results
  on it are not an independent test of B and C; the planted sets are.

**Stress set:** the 13 cases with their 1.1.0 ideals, plus 3 new cases (a treated death
with its cause, a single-animal neoplasm, relative organ weights with a 20% body weight
drop), in tests/fixtures/tox_stress under `ideal["1.1.0"]`, committed with this section;
run once at the end.

### E. Also in 1.1.0

- A canary leak check for the preclinical zone on kind (`meridian leakcheck --env kind
  --zone preclinical`), the phase 4b method: the canary planted in a copy of the SEND cut
  (MI's MIORRES, a column the agent never reads, so it cannot reach a finding), the zone
  run through the dispatcher, and every store searched with its positive control.
- scipy's Dunnett "precision loss" warning: found and fixed at its source, or recorded
  with why it is safe.

## 1.1.0 results

Run on 15 Nov 2026, preclinical.tox_study_monitor 1.1.0 at commit `9b2ebbd`, the
pre-registration (`b76c90c`) unchanged. Null and dev were run once each while building
(nothing was changed after them); holdout once; the stress set once. Scorecards:
`data/evals/preclinical/{null,dev,holdout}.json` (git-ignored), ground truth
`data/ground_truth/preclinical/`.

### Null cuts (20 control bases): statistical targets missed

| | Target | Result |
|---|---:|---:|
| Statistical findings per cut, mean | at most 5 | **5.95** (0 to 15) |
| dose_related_* per cut, mean | at most 2 | **2.3** |
| Cuts with more than 10 | none | **3** (12, 14, 15) |
| Rule findings: deaths / neoplasms | exactly the resampled ones / 0 | 56 of 56 expected / 0 |
| Red-cell cost (1.1.0 rule against a plain 10%) | at most 0.5 a cut | 0.2 against 0.05: **+0.15** |

Of the 119 statistical findings, **62 are recovery-only** (58 changes, 4 incidences) and
57 are main-study (2.85 a cut, within the target). Cause: each recovery group is 5 draws
with replacement from only 5 control animals per sex, so a group often holds the same
animal two or three times, its variance collapses and Dunnett's test fires (one dev copy
had 20 recovery-only findings, 19 of them in females). That is a property of the base I
pre-registered, and also a warning about 5-animal groups in real studies. Main-study
incidence trends (exact test) still give 1.5 a cut over about 70 lesion endpoints, which
is what a 5% one-sided level over that many endpoints gives. No rule was changed.

### Planted effects (dev and holdout, 10 copies each; holdout run once)

| Effect | Target M / F | Dev M / F | Holdout M / F |
|---|---:|---:|---:|
| ALT up, AST up | 95% / 95% | 100% / 100% | 100% / 100% |
| ALT and AST with mid and high flagged | 90% / 90% | 100% / 100% | 100% / 100% |
| RBC down 6% | 80% / 80% | 100% / 100% | 100% / 100% |
| HGB down 6% | 80% / 80% | 100% / 80% | 80% / **70%** |
| HCT down 6% | 80% / 80% | 100% / 100% | 80% / 100% |
| Body weight gain down 30% | 80% / 50% | **70%** / 50% | **60%** / 50% |
| Liver weight up 25% | 50% / 90% | **30%** / 90% | 60% / **60%** |
| Hepatocellular hypertrophy | 95% / 95% | 100% / 100% | 100% / 100% |
| Planted death raised with its MI cause | 100% / 100% | 100% / 100% | 100% / 100% |
| One-animal carcinoma raised | 100% / 100% | 100% / 100% | 100% / 100% |
| CREAT outlier raised | 0 copies | 0 | 0 |
| Statistical findings on unplanted endpoints, mean | at most 5 | 3.7 (max 20) | 2.6 (max 6) |

Holdout misses: male body weight gain (60% against 80%), female liver weight (60% against
90%), female HGB (70% against 80%). With the evaluation flaw of 1.0.0 removed, these are
power at n = 10 with this study's control variance (gain CV 19% M, 28% F; liver weight
CV 22% M, 14% F), not a rule failing; I did not measure how much the repeated animals of
the bootstrap add: the 1.0.0 "liver weight
F 10%" is now 60% to 90%.

### Stress set (16 cases, run once): 13 as ideal

`recovery_reverses` and `unit_change_in_lb` give the same chance extras as 1.0.0 (relative
kidney weight F, now `recovery_only_change`; body weight M). `lower_body_weight` misses the
male grouped finding: in that seeded draw the males' absolute organ weights at the high
dose came out about 8% lower by chance, so the relative weights rose only 13% with Dunnett
p 0.08 to 0.12 and none was flagged; the rule did what it says. The three new cases
(death with cause, single neoplasm, female body-weight grouping) are as ideal.

### The real study, in a Substrate sandbox on kind

One run on MRD-101-TOX-01 (SendStudyLoaded seq 19601), template
`preclinical-9afb2a96aecf-m384`, gVisor, 5.8 s, 0 failed: **98 findings** (PRE-0134 to
PRE-0231, seq 19828 to 19925), against the expected 50 to 90 (**missed**). By check:
dose_related_change 43, dose_related_incidence 20, pairwise_change 5, recovery_only_change
10, recovery_only_incidence 3, organ_weight_secondary_to_bw 2, neoplasm 4,
unscheduled_death 2, data quality 9. 1.0.0's 133 are all resolved: 75 superseded (the same
check raised by 1.1.0) and 58 retired (1.1.0 completed on TOX-FINAL without raising them).

| Reviewer's guide | 1.1.0 |
|---|---|
| Body weight and gain lower in groups 3 and 4 | Group 4: BW -18% M, -19% F, gain dose-related both sexes. Group 3 not (as expected, -2% / +2%) |
| AST, ALT, ALP higher in group 4 | AST and ALP dose-related M and F; ALT dose-related F, change without a trend M |
| RBC, HGB, HCT lower in group 4 | **Females:** HGB and HCT -9%, dose-related (HGB persists, HCT reverses at recovery). **Males, main study: not raised**: HCT -5% (Dunnett p 0.003) has no coherent partner (RBC -3.5% p 0.08, HGB -3.7% p 0.06), so the 10% magnitude applies. Males at recovery: RBC -11% and HCT -6%, recovery-only |
| Liver weights higher in group 4 | Females: absolute +90%, relative +131%, dose-related, persists. Males: relative +21% (absolute -1%) in the grouped "may be secondary to lower body weight" finding |
| Liver findings in groups 3 and 4 | Hypertrophy M (persists) and F (partly reverses); enlarged liver M and F; necrosis F. Vacuolization only as recovery-only (main M exact trend p 0.0507). Not raised, as expected: necrosis M |
| Liver tumours in group 4 | Neoplasm findings: adenoma M (2 of 10, exact p 0.058), carcinoma M (1 of 10, main) and F (1 of 5, recovery). Also: uterine leiomyoma F at 2 mg/kg (1 of 10) |
| Premature deaths in group 4 | Both raised with their MI cause: male 4003 (main, day 90) and female 4113 (recovery, day 100), each a moribund sacrifice, liver hepatocellular carcinoma. The control's gavage-error death is not raised (control) |

Covered against the targets: every guide signal except **the male red cell decrease in the
main study**. The real study's values were seen in 1.0.0's report, so this is not an
independent test of the 1.1.0 rules; the planted sets are.

### Also

- Leak check, preclinical zone on kind (`meridian leakcheck --env kind --zone
  preclinical`, 20 runs): canary in none of the six sources (dispatcher input store,
  dispatcher pod, its logs, the snapshot bucket, the node, the host's var/), every positive
  control found; `var/leakcheck/kind-20261115T081914Z.json`. A first attempt failed only on
  the bucket rule (the previous image's golden snapshots still there);
  `meridian substrate deploy --prune` deleted the 11 stale templates and the rerun passed.
- scipy's Dunnett warning: it fires whenever one sample is constant (urinalysis grades all
  5.0); that sample's variance is exactly 0 and the pooled variance comes from the others,
  so the result is exact. Silenced for that case only; a test checks the statistic against
  the pooled t computed by hand.
- Process: my first attempt at the pre-registration commit ran the test hook while I was
  already editing 1.1.0 code in the same tree, and failed. I stashed the code and committed
  the pre-registration alone (`b76c90c` holds only the doc, the fixtures, synth and their
  test); the code came after it.

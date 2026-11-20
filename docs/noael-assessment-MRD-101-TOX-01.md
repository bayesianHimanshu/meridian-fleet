# NOAEL assessment for MRD-101-TOX-01

**Status: recorded.**
- **Who did what:** Claude Code drafted this assessment from the agent's findings. Himanshu
  reviewed it and recorded the decisions through the Fleet API as Dr. Leela Nair
  (`leela.nair`, the toxicologist role in this demonstration).
- **Claude Code recorded nothing** and did not call the API as any person.
- **Ledger record:** the seq of every decision and dismissal is in the section "Ledger
  record" at the end. Where a borderline judgement is noted below, the recorded decisions
  follow the call this assessment proposed.

**What it rests on:**
- The 98 findings of `preclinical.tox_study_monitor` 1.1.0 (PRE-0134 to PRE-0231, ledger
  seq 19828 to 19925). No decision had been recorded on any of them when this assessment
  was drafted: the ledger then ended at seq 19926, the run's completion.
- The study's own tables (cut TOX-FINAL), read to get severity grades and the per-dose
  counts behind each finding.

**Study:**
- 13-week oral gavage study in Sprague-Dawley rats, followed by a 2-week recovery period.
- Main study: 10 rats per sex per group. Recovery: 5 per sex per group.
- Doses: vehicle, 2, 20 and 200 mg/kg/day.
- Severity grades in MI: 2 = mild, 3 = moderate. No grade 1 (minimal) is used.
- The data are synthetic (docs/preclinical-data.md). Some patterns look built rather than
  biological, for example identical microscopic counts in both sexes and brain weight 43%
  higher at 20 mg/kg/day. The classifications below treat the data at face value but note
  where this matters.

## 1. Findings by dose group and sex

How to read the lists:
- A finding is listed at every dose where the agent flagged a group, or where at least one
  treated animal had the tumour or died.
- `r` after an id means the finding is a main-study finding whose recovery group at that
  dose was also flagged. It is evidence on that finding, not a separate finding.
- Ids without `r` in the recovery rows are recovery-only findings, tumours or deaths.
- Not listed: data-quality findings PRE-0223 to PRE-0231. Too few control values existed to
  compare GGT, urine protein and two lung findings with. These are not toxicological
  findings, and the NOAEL does not rest on them.

| Sex | Phase | 2 mg/kg/day | 20 mg/kg/day | 200 mg/kg/day |
|---|---|---|---|---|
| M | main | PRE-0141, 0143, 0144, 0148, 0153, 0157, 0158 | PRE-0141, 0142, 0143, 0144, 0155, 0157, 0158, 0159 | PRE-0134 to 0159 (all 26), 0215, 0217, 0218, 0221 |
| M | recovery | PRE-0134r, 0135r, 0140r, 0206 | PRE-0138r, 0140r, 0202, 0205, 0206, 0209, 0210 | PRE-0134r, 0135r, 0136r, 0139r, 0142r, 0144r, 0146r, 0147r, 0149r, 0150r, 0151r, 0156r, 0202, 0203, 0204, 0205, 0207, 0208, 0209, 0210 |
| F | main | PRE-0173, 0194, 0199, 0200, 0220 | PRE-0165, 0166, 0169, 0172, 0173, 0174, 0175, 0176, 0177, 0178, 0179, 0181, 0182, 0183, 0184, 0185, 0186, 0189, 0190, 0192, 0193, 0196, 0198, 0199, 0200, 0201 | PRE-0160 to 0171, 0174 to 0188, 0190 to 0201, 0216 |
| F | recovery | PRE-0211 | PRE-0173r, 0174r, 0175r, 0176r, 0181r, 0183r, 0184r, 0185r, 0186r, 0189r, 0190r, 0211, 0214 | PRE-0160r, 0161r, 0164r, 0173r, 0175r to 0184r, 0186r to 0190r, 0211, 0212, 0213, 0214, 0219, 0222 |

### Changes in the data that the agent did not raise but that bear on the NOAEL

The agent's rules did not raise these, so they have no finding id. A toxicologist reading
the pathology tables would see them, and they affect the decision:

| Change | Counts (control, 2, 20, 200 mg/kg/day) | Why it matters |
|---|---|---|
| Liver necrosis, males, main study | 0/10, 0/10, **4/10**, 1/10, all mild; recovery 0/5, 0/5, 3/5, 1/5 | Adverse at 20 mg/kg/day in males, as in females (PRE-0198). Not raised because the count drops at 200, so there is no trend, and 4 of 10 against 0 of 10 is not significant on its own (Fisher p about 0.09). The reviewer's guide lists liver findings in groups 3 and 4. |
| Kidney, proximal tubule necrosis (focal), both sexes, main study | 0/10, **3/10**, 0/10, 2/10, all moderate | Moderate necrosis at the lowest dose is the main threat to a NOAEL of 2 mg/kg/day. It is absent at 20 and lower at 200. It is not in the reviewer's guide. See borderline judgement B1. |
| Liver inflammation, both sexes | 0/10, 0/10, 2/10, 0/10, mild | Supports the liver effect at 20 mg/kg/day. |

## 2. Classification of each finding

Categories:
- **Adverse:** harms the animal, or is a lesion that impairs function.
- **Adaptive:** an adaptive response without degeneration.
- **Non-adverse:** treatment-related or possibly so, but not harmful.
- **Incidental:** not treatment-related.

**Borderline** marks a call that a reasonable toxicologist could make the other way.

**Criteria applied:**
- Kerlin et al. 2016 and Palazzi et al. 2016: adversity is judged per finding by weight of
  evidence. Reversibility alone does not decide it.
- Lewis et al. 2002: adaptive response, transience, magnitude, and association with related
  endpoints.
- Hall et al. 2012: liver weight increase and hepatocellular hypertrophy without
  histological or clinical-pathology evidence of liver injury are adaptive and non-adverse.
- Everds et al. 2013: changes in leukocytes, adrenal, thymus and spleen can be secondary to
  stress or lower body weight.
- Sellers et al. 2007: organ weights are read with body weight and histopathology.
- **Body weight:** a decrease of 10% or more against control is taken as adverse. This is a
  common convention (it is also the agent's grouping threshold); I did not verify a single
  source for it.
- **Liver enzymes:** increases below about 2-fold without a correlating lesion are taken as
  non-adverse. This too is a common convention that I did not verify against a source.

### Males, main study

| Finding | Change (flagged doses) | Class | Reasoning |
|---|---|---|---|
| PRE-0134 body weight | 200: -18% (recovery -21%) | Adverse | Over 10% below control and persists after recovery. |
| PRE-0135 body weight gain | 200: -49% | Adverse | Same effect as PRE-0134. |
| PRE-0136 ALP | 200: +28% | Non-adverse | Under 2-fold. Part of the liver response at a dose already adverse. |
| PRE-0137 ALT | 200: +34%, reverses | Non-adverse | Under 2-fold, reversible, no trend. |
| PRE-0138 AST | 200: +41% (20: +20%, not flagged) | Non-adverse | Under 2-fold. Supports the liver effect. |
| PRE-0139 cholesterol | 200: +39% | Non-adverse | Consistent with hepatic enzyme induction (Hall 2012). |
| PRE-0140 creatinine | 200: +25%, reverses | Non-adverse, **borderline** | Small. Kidney necrosis does not rise with dose. |
| PRE-0141 large unstained cells | 2, 20, 200: -40 to -47%, no trend | Non-adverse | Flat across doses, a low-count parameter, part of the WBC decrease. |
| PRE-0142 lymphocytes | 20, 200: -27%, -34% | Non-adverse | Consistent with stress or lower body weight (Everds 2013). No lymphoid lesion. |
| PRE-0143 neutrophils | 2, 20, 200: about -33%, flat | Non-adverse | No dose response, and females change in the opposite direction. |
| PRE-0144 WBC | 2, 20, 200: -32, -29, -41% | Non-adverse, **borderline at 2** | Flagged at the lowest dose, but flat across a 100-fold dose range. Opposite in females, no lymphoid or bone-marrow lesion. |
| PRE-0145, 0146 pituitary weight | 200: +25% abs, +56% rel | Non-adverse | No microscopic correlate raised. Relative weight is inflated by the lower body weight. |
| PRE-0147 heart weight, absolute | 200: -32% | Non-adverse, **borderline** | Larger than the body weight decrease. Heart inflammation is present but not dose-graded. |
| PRE-0148 heart weight, relative | 2: -13%, 200: -17% | Non-adverse | At 2 there is no dose response (20 is -7%, not flagged). |
| PRE-0149 kidney weight, absolute | 200: -33% | Non-adverse, **borderline** | Mostly lower body weight. No dose-related kidney lesion. |
| PRE-0150 testis weight, absolute | 200: -11% | Non-adverse | Small, with no testicular lesion raised. |
| PRE-0151 enlarged liver (gross) | 200: 4/10 | Adaptive | Gross correlate of hypertrophy (and of the tumours, which are classed separately). |
| PRE-0152 skin matting | 200: 3/10, reverses | Non-adverse | Grooming change in animals in poor condition. Reversible. |
| PRE-0153 adrenal vacuolization | 2: 9/10, 20: 0/10, 200: 10/10 (control 1/10), mild | Non-adverse, **borderline at 2** | Adrenal cortical vacuolization is commonly a non-adverse, stress-related change (Everds 2013). The 0/10 at 20 makes the 2 mg/kg/day result inconsistent with a dose response. |
| PRE-0154 mammary atrophy | 200: 5/10, mild to moderate | Non-adverse, **borderline** | Seen only at a dose with about 18% lower body weight. Probably secondary. |
| PRE-0155 heart inflammation | 20: 3/10, 200: 3/10 (control 0/10), mild | Adverse, **borderline** | An inflammatory lesion in the myocardium, absent in controls. However, it is mild and does not rise from 20 to 200. Spontaneous cardiomyopathy is common in male SD rats. |
| PRE-0156 liver hypertrophy | 200: 6/10, persists | Adaptive | Hypertrophy alone is adaptive (Hall 2012). At 200 it comes with tumours and deaths, which are classed adverse on their own. |
| PRE-0157 alveolar macrophages | 2, 20, 200: 4 to 6/10 (control 1/10), mild | Non-adverse | Flat response. A common background finding in gavage studies. |
| PRE-0158 Peyer's patch mineralization | 2, 20, 200: 3, 3, 6/10, mild | Incidental | Mild, with no tissue damage or functional consequence. Treatment relation is doubtful. |
| PRE-0159 spleen pigmentation | 20, 200: 7, 8/10 (control 2/10) | Non-adverse | Pigment (presumably haemosiderin) from red-cell turnover. Mild, and reverses. |
| PRE-0215 relative organ weights grouped | 200 | Non-adverse | Absolute weights are not changed. These reflect body weight 18% lower (Sellers 2007). |
| PRE-0217 hepatocellular adenoma | 200: 2/10 (exact trend p 0.058) | Adverse | Liver tumours within 13 weeks, alongside carcinoma at the same dose. Treatment-related regardless of the p value. |
| PRE-0218 hepatocellular carcinoma | 200: 1/10 | Adverse | A malignant tumour, and it caused death (PRE-0221). |
| PRE-0221 death, animal 4003 | 200, day 90, moribund | Adverse | Unscheduled death caused by hepatocellular carcinoma. |

### Males, recovery-only findings

| Finding | Change | Class | Reasoning |
|---|---|---|---|
| PRE-0202 basophils | 20, 200: -43%, -62% | Non-adverse | Low-count parameter. Groups of 5. |
| PRE-0203 bilirubin | 200: +65% | Non-adverse, **borderline** | Could reflect the liver tumours. Small absolute change, groups of 5. |
| PRE-0204 HCT | 200: -6% | Non-adverse | Mild. |
| PRE-0205 RBC | 20, 200: -12%, -11% | Non-adverse, **borderline** | Over 10%, but seen only after recovery, in groups of 5, and not at the end of dosing. |
| PRE-0206 urea nitrogen | 2, 20: +30%, +39%, no trend | Incidental | No dose response (200 is +17%, not flagged). |
| PRE-0207 kidney weight, relative | 200: -23% | Non-adverse | Body-weight related, no correlate. |
| PRE-0208 spleen weight, absolute | 200: -34% | Non-adverse | Body-weight related, no correlate. |
| PRE-0209 skin crust (clinical sign) | 20, 200: 2/5 each | Incidental | Common, minor skin lesion, recovery only. |
| PRE-0210 liver vacuolization | 20: 3/5, 200: 2/5 | Non-adverse, **borderline** | Present only after recovery. Mild at 20. Accompanies liver necrosis that persisted at 20. |

### Females, main study

| Finding | Change (flagged doses) | Class | Reasoning |
|---|---|---|---|
| PRE-0160 body weight | 200: -19% | Adverse | Over 10% below control and persists. |
| PRE-0161 body weight gain | 200: -58% | Adverse | Same effect as PRE-0160. |
| PRE-0162 albumin/globulin ratio | 200: -21% | Non-adverse | Small, no correlate. |
| PRE-0163 ALP | 200: +52% | Non-adverse | Under 2-fold. Part of the liver response. |
| PRE-0164 ALT | 200: +25% | Non-adverse | Under 2-fold. |
| PRE-0165 AST | 20: +30%, 200: +56% | Non-adverse alone | Under 2-fold, but supports the necrosis at 20 (PRE-0198). Cited as supporting evidence for the LOAEL. |
| PRE-0166 basophils | 20, 200 | Non-adverse | Low-count parameter. |
| PRE-0167 HCT | 200: -9%, reverses | Non-adverse, **borderline** | Under 10% and reversible. Part of the red-cell decrease. |
| PRE-0168 HGB | 200: -9%, persists (-6%) | Non-adverse, **borderline** | Mild. At a dose already adverse. |
| PRE-0169 lymphocytes | 20, 200: +55%, +71% | Non-adverse | Possibly an inflammatory response to the liver lesions. No lymphoid lesion. |
| PRE-0170 neutrophils | 200: +107% | Non-adverse | Consistent with inflammation from the liver necrosis and tumours. |
| PRE-0171 prothrombin time | 200: +24% | Non-adverse | Mild. Could reflect liver function at 200. |
| PRE-0172 RDW | 20: +12%, no trend | Non-adverse | No dose response. |
| PRE-0173 reticulocytes | 2, 20: +42%, +44%; 200: -2% | Non-adverse | No dose response, and a regenerative change rather than a lesion. |
| PRE-0174 WBC | 20, 200: +60%, +78% | Non-adverse | Inflammatory, as for PRE-0169 and PRE-0170. |
| PRE-0175, 0176 brain weight | 20, 200: +43% abs | Non-adverse | Biologically implausible (brain weight is conserved), with no lesion. Probably an artefact of the synthetic data. |
| PRE-0177, 0178 adrenal weight | 20, 200 | Non-adverse | Consistent with stress (Everds 2013). |
| PRE-0179, 0180 pituitary weight | 20 (abs), 200 | Non-adverse | No correlate. |
| PRE-0181, 0182 thyroid weight | 20, 200: +58% abs at 20 | Adaptive | Consistent with hepatic enzyme induction increasing thyroid hormone clearance. No thyroid lesion was raised. |
| PRE-0183, 0184 heart weight | 20, 200: about +49% abs | Non-adverse, **borderline** | Large, with heart inflammation at the same doses. No degenerative lesion was raised. |
| PRE-0185, 0186 kidney weight | 20, 200 | Non-adverse, **borderline** | No dose-related kidney lesion. |
| PRE-0187, 0188 liver weight | 200: +90% abs, +131% rel, persists | Adaptive, **borderline** | Correlates with hypertrophy (Hall 2012). However, the magnitude is large and comes with necrosis and tumours, so many would call it adverse at 200. The NOAEL does not change either way. |
| PRE-0189 spleen weight, absolute | 20: +43%, no trend | Non-adverse | No dose response. |
| PRE-0190 spleen weight, relative | 20, 200 | Non-adverse | Pigmentation, no lesion. |
| PRE-0191 enlarged liver (gross) | 200: 5/10 | Adaptive | Gross correlate of hypertrophy. |
| PRE-0192 urinary bladder, dark red contents | 20: 2/10, 200: 6/10 | Non-adverse, **borderline** | Could indicate haematuria. No bladder or kidney lesion rises with dose. |
| PRE-0193 uterus, clear fluid | 20, 200 | Non-adverse | Usually reflects the stage of the oestrous cycle. |
| PRE-0194 adrenal vacuolization | 2: 9/10, 20: 0/10, 200: 10/10 | Non-adverse, **borderline at 2** | As in males (PRE-0153). |
| PRE-0195 mammary atrophy | 200: 5/10 | Non-adverse, **borderline** | Probably secondary to body weight 19% lower. A hormonal effect cannot be ruled out. |
| PRE-0196 heart inflammation | 20, 200: 3/10 each, mild | Adverse, **borderline** | As in males (PRE-0155). |
| PRE-0197 liver hypertrophy | 200: 9/10, partly reverses | Adaptive | Hall 2012, as for males. |
| PRE-0198 liver necrosis | 20: 4/10, 200: 2/10, mild, persists | **Adverse** | Necrosis is cell death, a degenerative lesion, and it is still present after recovery. With higher AST, it sets the LOAEL. |
| PRE-0199 alveolar macrophages | 2, 20, 200 | Non-adverse | As in males. |
| PRE-0200 Peyer's patch mineralization | 2, 20, 200 | Incidental | As in males. |
| PRE-0201 spleen pigmentation | 20, 200 | Non-adverse | As in males. |
| PRE-0216 relative organ weights grouped | 200 (ovary) | Non-adverse | Body-weight related. |
| PRE-0220 uterine leiomyoma | 2: 1/10 only | **Incidental** | A single benign tumour at the lowest dose. None at 20 or 200, no preneoplastic change, a different organ from the treatment-related liver tumours. Exact trend p 0.75. |

### Females, recovery-only findings, tumours and deaths

| Finding | Change | Class | Reasoning |
|---|---|---|---|
| PRE-0211 APTT | 2, 20, 200: +15 to 18%, flat | Non-adverse | Small, with no dose response. Groups of 5. |
| PRE-0212, 0213 thymus weight | 200: +72% abs | Non-adverse | Recovery rebound after body weight loss (Everds 2013). Groups of 5. |
| PRE-0214 liver vacuolization | 20: 4/5, 200: 2/5 | Non-adverse, **borderline** | As in males (PRE-0210). |
| PRE-0219 hepatocellular carcinoma | 200 recovery: 1/5 | Adverse | A malignant liver tumour, and it caused death (PRE-0222). |
| PRE-0222 death, animal 4113 | 200, day 100, moribund | Adverse | Unscheduled death caused by hepatocellular carcinoma. |

### Borderline calls that would change the NOAEL

- **B1. Kidney tubular necrosis at 2 mg/kg/day.**
  - What the data show: moderate, focal, 3 of 10 per sex. Not raised by the agent, and with
    no finding id.
  - Call (as recorded): not treatment-related, because it is absent at 20 mg/kg/day and
    lower at 200, and creatinine rises only at 200.
  - Had it been judged treatment-related, it would be adverse: the NOAEL would be **not
    established** and the LOAEL 2 mg/kg/day. Historical control data for this laboratory and strain would
    settle it, but the study has none.
- **B2. Adrenal cortical vacuolization at 2 mg/kg/day** (PRE-0153, PRE-0194).
  - 9 of 10 per sex, against 1 of 10 in controls.
  - Call (as recorded): non-adverse (stress-related, mild, and absent at 20).
  - Had it been judged adverse, the NOAEL would not be established.
- **B3. Lower white cells in males at 2 mg/kg/day** (PRE-0144, PRE-0143, PRE-0141).
  - Call (as recorded): non-adverse (flat across doses, opposite in females, no lesion).
- **B4. Heart inflammation at 20 mg/kg/day** (PRE-0155, PRE-0196).
  - Call (as recorded): adverse.
  - Had it been judged non-adverse:
    - Females still have a LOAEL of 20 (liver necrosis, PRE-0198).
    - Males have a LOAEL of 20 only on the unraised liver necrosis and the recovery liver
      changes (PRE-0210).
    - Otherwise the male NOAEL would rise to 20 and the male LOAEL to 200.

## 3. NOAEL and LOAEL

| Sex | NOAEL | LOAEL | LOAEL rests on (finding ids) | Notes |
|---|---|---|---|---|
| M | 2 mg/kg/day | 20 mg/kg/day | PRE-0155 (heart inflammation); supporting: PRE-0210 (liver vacuolization at recovery) | Also rests on liver necrosis in 4 of 10 males at 20, which the agent did not raise and which therefore has no id. At 200: PRE-0134, 0135, 0217, 0218, 0221. |
| F | 2 mg/kg/day | 20 mg/kg/day | PRE-0198 (liver necrosis), PRE-0165 (AST), PRE-0196 (heart inflammation); supporting: PRE-0214 | At 200: PRE-0160, 0161, 0219, 0222. |
| Combined | 2 mg/kg/day | 20 mg/kg/day | PRE-0198, PRE-0165, PRE-0155, PRE-0196 | The lower of the two sexes; both are the same. |

**Not adverse at 2 mg/kg/day:** PRE-0141, 0143, 0144, 0148, 0153, 0157, 0158, 0173, 0194,
0199, 0200 and 0220, each for the reason in section 2. The NOAEL holds only if B1 and B2
are accepted as called above (they were).

**As recorded** (docs/noael-decision.md):
- Three decisions, for sex `M`, `F` and `combined`.
- Each has `established` true, NOAEL 2 mg/kg and LOAEL 20 mg/kg.
- Each cites the finding ids in the table above:
  - M: PRE-0155 and PRE-0210.
  - F: PRE-0198, PRE-0165, PRE-0196 and PRE-0214.
  - Combined: PRE-0198, PRE-0165, PRE-0155 and PRE-0196.

## 4. Rationale (5 sentences; recorded on all three decisions)

> In this 13-week oral gavage study in Sprague-Dawley rats, 200 mg/kg/day caused
> unscheduled deaths from hepatocellular carcinoma in both sexes (PRE-0221, PRE-0222),
> liver tumours (PRE-0217, PRE-0218, PRE-0219) and body weight 18 to 19% below control
> (PRE-0134, PRE-0160). At 20 mg/kg/day, hepatocellular necrosis (PRE-0198; also 4 of 10
> males in the pathology data) with higher AST (PRE-0165) and myocardial inflammation in
> both sexes (PRE-0155, PRE-0196) were considered adverse, and liver changes remained after
> recovery (PRE-0210, PRE-0214). Changes at 2 mg/kg/day (adrenal cortical vacuolization,
> lower leukocyte counts in males, alveolar macrophages, Peyer's patch mineralization and a
> single uterine leiomyoma) lacked a dose response or a correlating lesion and were
> considered non-adverse or incidental, as was focal renal tubular necrosis in 3 of 10 rats
> per sex at 2 mg/kg/day, which was absent at 20 mg/kg/day. The NOAEL is therefore
> 2 mg/kg/day and the LOAEL 20 mg/kg/day in males, females and combined. Assessment drafted
> by Claude Code from the agent's findings; recorded as Dr. Leela Nair, played by Himanshu,
> in this demonstration.

**About the recorded text:**
- The same text is recorded on all three decisions.
- It matches the text above except for typographic apostrophes (’ in "Peyer’s" and
  "agent’s").

## 5. Dismissal reasons for incidental findings

All are under the API's 2000-character limit.
- **Dismissed:** PRE-0220, PRE-0206 and PRE-0209, each from its own finding page, with the
  reasons below (seqs in "Ledger record").
- **Not dismissed:** PRE-0158 and PRE-0200 are left open (see the last paragraph of this
  section).

- **PRE-0220 (uterine leiomyoma, females, 2 mg/kg/day):** "Single benign uterine leiomyoma
  in 1 of 10 females at the lowest dose (2 mg/kg/day), with none at 20 or 200 mg/kg/day, no
  preneoplastic or proliferative uterine change in any group, and an exact trend p of 0.75.
  It is in a different organ from the treatment-related liver tumours. Considered a
  spontaneous, incidental tumour and not related to treatment."
- **PRE-0158 (Peyer's patch mineralization, males) and PRE-0200 (females):** "Mild
  mineralization in 3, 3 and 6 of 10 animals at 2, 20 and 200 mg/kg/day, with no
  associated inflammation, necrosis or functional change. Mineralization of this kind is a
  common background change of no toxicological significance. Considered incidental and not
  adverse."
- **PRE-0206 (urea nitrogen, males, recovery only):** "Higher at 2 and 20 mg/kg/day after
  recovery only (groups of 5) but not at 200 mg/kg/day, with no trend and no change at the
  end of dosing or in kidney histopathology. Considered incidental."
- **PRE-0209 (skin crust, males, recovery only):** "Crust in 2 of 5 males at 20 and at
  200 mg/kg/day after recovery only, absent at the end of dosing, with no related skin
  pathology raised. A common minor clinical sign in group-housed rats. Considered
  incidental."

PRE-0158 and PRE-0200 were not dismissed: their counts rise with dose, so they are left
open as non-adverse rather than called incidental. The NOAEL does not depend on them.

## Sources

- Kerlin R, et al. Scientific and Regulatory Policy Committee: Recommended ("Best")
  Practices for Determining, Communicating, and Using Adverse Effect Data from Nonclinical
  Studies. Toxicol Pathol 2016;44(2):147-162. doi:10.1177/0192623315623265.
- Palazzi X, et al. Characterizing "Adversity" of Pathology Findings in Nonclinical
  Toxicity Studies: Results from the 4th ESTP International Expert Workshop. Toxicol Pathol
  2016;44(6):810-824. doi:10.1177/0192623316642527.
- Hall AP, et al. Liver hypertrophy: a review of adaptive (adverse and non-adverse)
  changes, conclusions from the 3rd International ESTP Expert Workshop. Toxicol Pathol
  2012;40(7):971-994. doi:10.1177/0192623312448935.
- Lewis RW, et al. Recognition of adverse and nonadverse effects in toxicity studies.
  Toxicol Pathol 2002;30(1):66-74. doi:10.1080/01926230252824725.
- Everds NE, et al. Interpreting stress responses during routine toxicity studies: a review
  of the biology, impact, and assessment. Toxicol Pathol 2013;41(4):560-614.
  doi:10.1177/0192623312466452.
- Sellers RS, et al. Society of Toxicologic Pathology position paper: organ weight
  recommendations for toxicology studies. Toxicol Pathol 2007;35(5):751-755.
  doi:10.1080/01926230701595300.
- PointCross PC201708 reviewer's guide (`PC201708_draft_nsdrg.docx`, section 6.2), as
  recorded in docs/preclinical-data.md.

**How these sources were checked:** each reference's bibliographic details were confirmed
online from abstracts and index records. The full texts were not read, so the criteria
above are cited at the level of their abstracts.

## Ledger record

All events were recorded by `leela.nair` through the Fleet API on 2026-11-19 (UTC). After
the last of them, `meridian ledger verify` reports the chain intact: 19,946 events checked,
no broken seq.

### Current NOAEL decisions

| Seq | Decision | Sex | NOAEL | LOAEL | Findings | Revises |
|---:|---|---|---|---|---|---|
| 19941 | NOAEL-a80dc7384e52 | M | 2 mg/kg | 20 mg/kg | PRE-0210, PRE-0155 | NOAEL-2991c4adeac8 |
| 19942 | NOAEL-347f3178eba5 | F | 2 mg/kg | 20 mg/kg | PRE-0214, PRE-0196, PRE-0198, PRE-0165 | NOAEL-ed6d967d42eb |
| 19943 | NOAEL-cd4154d72a20 | combined | 2 mg/kg | 20 mg/kg | PRE-0198, PRE-0196, PRE-0165, PRE-0155 | NOAEL-4c392e7b81b9 |

### Dismissals

| Seq | Finding | Action | Reason |
|---:|---|---|---|
| 19944 | PRE-0220 (uterine leiomyoma, F, 2 mg/kg/day) | dismissed | the reason in section 5 |
| 19945 | PRE-0209 (skin crust, M, recovery) | dismissed | the reason in section 5 |
| 19946 | PRE-0206 (urea nitrogen, M, recovery) | dismissed | the reason in section 5 |

### Superseded attempts (seq 19927 to 19940)

The NOAEL form was first used one finding at a time.
- **What went wrong:**
  - Each submission named a single finding and carried that finding's one-line reasoning
    from section 2 as its rationale, rather than the rationale in section 4.
  - The dismissal reasons were then entered into the NOAEL form instead of on the finding
    pages, so the last three attempts recorded "NOAEL not established".
- **How it was fixed:** the form names the current decision as the one it revises, so every
  attempt replaced the one before it. The three current decisions above revise the last of
  them.
- **Where they stay:** all 14 remain in the ledger as history (append-only). None is current.

| Seq | Decision | Sex | Recorded as | Findings | Rationale entered | Superseded by |
|---:|---|---|---|---|---|---|
| 19927 | NOAEL-6be0babf5854 | M | NOAEL 2, LOAEL 20 | PRE-0155 | heart inflammation line (section 2) | NOAEL-de68d9bc7258 |
| 19928 | NOAEL-de68d9bc7258 | M | NOAEL 2, LOAEL 20 | PRE-0210 | liver vacuolization line | NOAEL-2934f94b5caa |
| 19929 | NOAEL-1a0400f86421 | F | NOAEL 2, LOAEL 20 | PRE-0198 | liver necrosis line | NOAEL-ed5a86275838 |
| 19930 | NOAEL-ed5a86275838 | F | NOAEL 2, LOAEL 20 | PRE-0165 | AST line | NOAEL-491d86249658 |
| 19931 | NOAEL-491d86249658 | F | NOAEL 2, LOAEL 20 | PRE-0196 | "As in males (PRE-0155)." | NOAEL-f6a2cad64e6e |
| 19932 | NOAEL-f6a2cad64e6e | F | NOAEL 2, LOAEL 20 | PRE-0214 | "As in males (PRE-0210)." | NOAEL-61e71dcd54ae |
| 19933 | NOAEL-79fd1a4a4910 | combined | NOAEL 2, LOAEL 20 | PRE-0198 | liver necrosis line | NOAEL-d5badca75344 |
| 19934 | NOAEL-d5badca75344 | combined | NOAEL 2, LOAEL 20 | PRE-0165 | AST line | NOAEL-168da18e69f2 |
| 19935 | NOAEL-168da18e69f2 | combined | NOAEL 2, LOAEL 20 | PRE-0155 | heart inflammation line | NOAEL-4c392e7b81b9 |
| 19936 | NOAEL-4c392e7b81b9 | combined | NOAEL 2, LOAEL 20 | PRE-0196 | heart inflammation line | NOAEL-cd4154d72a20 (current) |
| 19937 | NOAEL-61e71dcd54ae | F | not established, LOAEL 2 | PRE-0206 (a male finding) | leiomyoma dismissal reason | NOAEL-ed6d967d42eb |
| 19938 | NOAEL-ed6d967d42eb | F | not established, no LOAEL | PRE-0200, PRE-0158 | mineralization dismissal reason | NOAEL-347f3178eba5 (current) |
| 19939 | NOAEL-2934f94b5caa | M | not established, no LOAEL | PRE-0206 | urea nitrogen dismissal reason | NOAEL-2991c4adeac8 |
| 19940 | NOAEL-2991c4adeac8 | M | not established, no LOAEL | PRE-0209 | skin crust dismissal reason | NOAEL-a80dc7384e52 (current) |

The form accepted three inputs that would have flagged these attempts, and none of them is
changed here:
- a revision of the current decision, without asking at submit time;
- "not established" with no LOAEL;
- a finding of the other sex.

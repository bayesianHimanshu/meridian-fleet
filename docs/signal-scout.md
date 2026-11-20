# portfolio.safety_signal_scout: pre-registration and results

Phase 5 step 3a. **Everything above "Results" was committed before the agent's code and
before any disproportionality was computed** (`git log --follow` on this file).

**What was seen before, said now:** to find the query terms I ran count queries by hand
(below): the generic and brand names, the totals, and the drug's top four reaction terms
by count (nausea 593, vomiting 439, constipation 202, off label use 195). No PRR, ROR or
chi-squared was computed, and no other term was looked at, before this file was committed.

## What it says and does not say

It reports **signals of disproportionate reporting** in FDA's adverse event reporting
system (FAERS), through openFDA, for the drug studied in MRD-201: xanomeline, marketed in
the US as xanomeline and trospium chloride (brand name in the citations below). A signal says a reaction term is reported with
the drug more often, relative to all its reports, than with all other drugs. It is never
an incidence, a rate or a causal claim; every finding carries FAERS's own caveats as
evidence. The medical monitor decides each one.

## Data and queries (openFDA drug adverse event endpoint)

- **Product:** `patient.drug.openfda.generic_name.exact:"XANOMELINE AND TROSPIUM CHLORIDE"`.
  Found by count queries on 20 Nov 2026 (data date `meta.last_updated` 2026-10-28):
  `patient.drug.openfda.generic_name:xanomeline` counted by `generic_name.exact` gives one
  xanomeline term, `XANOMELINE AND TROSPIUM CHLORIDE`, in 2,767 reports; the same reports
  by `brand_name.exact` are `COBENFY`. Left out: reports that name the product only in the
  free-text `medicinalproduct` field (`TROSPIUM CHLORIDE\XANOMELINE TARTRATE` 75,
  `TROSPIUM CHLORIDE\XANOMELINE` 27; some may also carry the harmonised name). Any role of
  the drug in the report (suspect, concomitant, interacting) counts, as openFDA counts it.
- **Report totals** come from `count=fulfillexpeditecriteria`, summed: the one field found
  on every report (2,767 of 2,767 for the drug; 21,054,158 in all of FAERS, against
  21,044,294 for `serious` and 17,860,952 for `reporttype`, which some reports lack).
- **Queries, count queries only, no API key:**
  1. the drug's reaction terms: `search=<product>&count=patient.reaction.reactionmeddrapt.exact&limit=999`
     (999 is the largest limit openFDA accepts without a key; the drug has 659 terms);
  2. the drug's reports: `search=<product>&count=fulfillexpeditecriteria`;
  3. all reports: `count=fulfillexpeditecriteria`;
  4. for each term with at least 3 reports with the drug, all its reports:
     `search=patient.reaction.reactionmeddrapt.exact:"<term>"&count=fulfillexpeditecriteria`.
  About 250 term queries plus 3; openFDA's limits without a key are 240 requests a minute
  and 1,000 a day per IP address; the agent waits 0.3 s between requests and stops at 900.
- **The label:** `drug/label.json?search=openfda.generic_name.exact:"XANOMELINE AND
  TROSPIUM CHLORIDE"&limit=1` (a search query on the label endpoint).

## Method (pre-registered)

For each reaction term R with the drug D, the 2 x 2 table of reports:
a = D and R (query 1), b = D without R (query 2 minus a), c = R without D (query 4 minus
a), d = neither (query 3 minus a, b and c).

- **PRR** = [a / (a + b)] / [c / (c + d)]; 95% CI exp(ln PRR +- 1.96 SE), SE =
  sqrt(1/a - 1/(a+b) + 1/c - 1/(c+d)).
- **ROR** = (a d) / (b c); 95% CI exp(ln ROR +- 1.96 SE), SE = sqrt(1/a + 1/b + 1/c + 1/d).
- **Chi-squared**, 1 degree of freedom, with Yates's continuity correction:
  N (|a d - b c| - N/2)^2 / ((a+b)(c+d)(a+c)(b+d)).
- **Signal of disproportionate reporting:** a >= 3 **and** PRR >= 2 **and** chi-squared
  >= 4. Source: Evans SJ, Waller PC, Davis S. Use of proportional reporting ratios (PRRs)
  for signal generation from spontaneous adverse drug reaction reports. Pharmacoepidemiol
  Drug Saf 2001;10(6):483-6, doi:10.1002/pds.677, whose abstract states "as minimum
  criteria for a signal, 3 or more cases, PRR at least 2, chi-squared of at least 4". The
  abstract does not say which chi-squared; Yates's correction is chosen here (it is the
  smaller value, so the stricter rule). The ROR and the CIs are reported, not used in the
  rule; van Puijenbroek EP et al., Pharmacoepidemiol Drug Saf 2002;11(1):3-10,
  doi:10.1002/pds.668, compares these measures ("ROR - 1.96 SE", "PRR - 1.96 SE").
- Every term is reported as openFDA codes it (MedDRA preferred terms), including terms
  about use rather than harm (for example OFF LABEL USE): no term is excluded.
- One finding per signal, check `reporting_signal:<term>`, severity low, visible to the
  medical monitor and the portfolio lead, decided by the medical monitor. A newer scan
  supersedes the same term's finding of an older one; a term no longer a signal is
  retired.

## The label comparison (an evaluation not written by us)

The US label of xanomeline and trospium chloride (from the label endpoint; US label for COBENFY, openFDA set id 8f0e73bf-6025-44f6-ab64-0983322de0df, version 6, effective 2026-01-16) was written by the sponsor and FDA, not by
this project. For each signal, the term is looked for in the label's adverse reactions,
warnings and precautions and boxed warning text: **listed** when the term, lower-cased,
appears as a phrase in that text (lower-cased, punctuation as spaces); else **not found**.
No synonyms and no MedDRA hierarchy: "GASTROOESOPHAGEAL REFLUX DISEASE" is not found in a
label that says "gastrointestinal reflux disease". Reported both ways: the share of
signals listed, and the label's most common adverse reactions (section 6, "incidence >= 5%
and at least twice placebo") with whether each is a signal. As they are; no target.

## FAERS caveats (quoted from open.fda.gov/apis/drug/event, as evidence on every finding)

- "There is no certainty that the reported event (adverse event or medication error) was
  actually due to the product."
- "FDA does not require that a causal relationship between a product and event be proven"
- "FDA does not receive reports for every adverse event or medication error that occurs
  with a product."
- "Many factors can influence whether or not an event will be reported"
- "The information in these reports has not been scientifically or otherwise verified as
  to a cause and effect relationship and cannot be used to estimate the incidence of these
  events."

## Reproducibility

Live data changes. Each run records, on its ActorWoke, every query with the sha256 of its
response body and openFDA's `meta.last_updated`; the responses are kept as the run's
inputs (`var/signals/<trigger seq>/signal/openfda_responses.parquet`), and a rerun on them
(`meridian signals replay`) must give identical drafts.

## Results

Run on 19 Nov 2026 (UTC), portfolio.safety_signal_scout 1.0.0 at commit `eae0dee`, the
pre-registration (`11c2e8e`) unchanged; in an OpenShell sandbox under
policies/portfolio_signal.yaml, image built from `eae0dee` (clean).

### Conformance of portfolio_signal (OpenShell, ledger seq 19967 to 19984): pass

| Check | Expected | Result |
|---|---|---|
| openfda_count: GET api.fda.gov/drug/event.json?count=fulfillexpeditecriteria | allowed | status 200 |
| openfda_other_path: GET api.fda.gov/drug/ndc.json | refused | HTTP 403 policy_denied |
| egress_anthropic: GET api.anthropic.com/v1/models | refused | ConnectError, Errno 13; log: DENIED, transparent_tcp_policy_denied |
| egress_ollama: GET 172.17.0.1:11434/api/tags (the host Ollama) | refused | ConnectError, Errno 13; log: DENIED |
| egress_example_com | refused | ConnectError, Errno 13; log: DENIED |
| writes to /var/tmp and /opt/meridian; TCP to 192.0.2.1:443 | refused | Permission denied; log line found |
| /stage/meta and /stage/signal | readable | readable |
| every other /stage area (10) | refused | Permission denied |

`meridian policy prove`: portfolio_signal within the signal boundary. The two ALLOWED log
lines are api.fda.gov:443 under policy openfda.

### The scan (SignalScanRequested seq 19985, scan SCAN-20261119T211657Z)

- openFDA data date (`meta.last_updated`) **2026-10-28**; 2,767 reports with the drug of
  21,054,158; 659 reaction terms with the drug, 250 with at least 3 reports.
- 254 requests: 250 answered 200, **4 answered "Too Many Requests"** (HTTP 500) at 0.3 s
  between requests: DRUG INTERACTION, HICCUPS, HYPERSEXUALITY, INABILITY TO AFFORD
  MEDICATION. 1.0.0 does not retry (none was pre-registered), so those 4 are not tested;
  the view lists them. **246 terms tested, 76 signals** (SIG-0001 to SIG-0076, seq 19986
  onward), all open.
- Every response is kept (`var/signals/19985/signal/openfda_responses.parquet`) and named
  on the ActorWoke by path, query, status, sha256 and data date. `meridian signals replay
  --trigger 19985`: 76 drafts, sha256 `49dd00f1e358fdcb`, **identical** to the recorded run
  (run again after the query encoder changed in `3097be2`: still identical).

The 15 strongest signals by PRR (all 76 on the Safety signals view):

| Term | Reports (a) | PRR (95% CI) | ROR | Chi-squared | In the label text |
|---|---:|---|---:|---:|---|
| drooling | 58 | 64.4 (49.8 to 83.1) | 65.7 | 3,526 | listed |
| psychiatric decompensation | 11 | 43.0 (23.8 to 77.7) | 43.2 | 408 | not found |
| intrusive thoughts | 6 | 39.0 (17.5 to 86.8) | 39.0 | 185 | not found |
| soliloquy | 4 | 35.4 (13.2 to 94.3) | 35.4 | 101 | not found |
| urinary retention | 153 | 35.0 (30.0 to 40.9) | 37.0 | 5,007 | listed |
| blunted affect | 3 | 31.1 (10.0 to 96.5) | 31.1 | 60 | not found |
| hallucination, auditory | 56 | 26.9 (20.7 to 34.9) | 27.4 | 1,366 | not found |
| vomiting projectile | 10 | 26.4 (14.2 to 49.0) | 26.5 | 219 | not found |
| salivary hypersecretion | 31 | 23.6 (16.6 to 33.6) | 23.9 | 648 | listed |
| inappropriate affect | 4 | 19.2 (7.2 to 51.3) | 19.3 | 52 | not found |
| loss of therapeutic response | 7 | 16.7 (8.0 to 35.1) | 16.8 | 88 | not found |
| schizophrenia | 26 | 16.3 (11.1 to 23.9) | 16.4 | 358 | listed (as the indication) |
| psychotic disorder | 62 | 16.0 (12.5 to 20.5) | 16.3 | 856 | not found |
| libido increased | 4 | 15.0 (5.6 to 40.0) | 15.0 | 39 | not found |
| violence-related symptom | 3 | 14.8 (4.8 to 45.8) | 14.8 | 26 | not found |

### Against the US label (COBENFY, set 8f0e73bf-6025-44f6-ab64-0983322de0df, version 6, effective 2026-01-16)

**19 of 76 signals are named in the label's text** (exact phrase): nausea, vomiting,
constipation, urinary retention, dyspepsia, vision blurred, dry mouth, heart rate
increased, somnolence, drooling, abdominal discomfort, tachycardia, salivary
hypersecretion, dysuria, schizophrenia, dyskinesia, akathisia, hypertensive crisis,
urinary hesitation. "Schizophrenia" matches because the label names the indication, not
because it lists it as a reaction: the exact-phrase rule cannot tell them apart.

The label's ten most common adverse reactions (section 6.1, incidence at least 5% and at
least twice placebo) against the signals: **5 are signals** (nausea, dyspepsia,
constipation, vomiting, tachycardia); 5 are not under the rule as written: hypertension,
abdominal pain, diarrhea, dizziness, and gastrointestinal reflux disease (its MedDRA
term, GASTROOESOPHAGEAL REFLUX DISEASE, is a signal at PRR 5.9, and ABDOMINAL DISCOMFORT
is one at 2.6, but the exact-phrase rule does not equate them).

What the 57 signals not found in the label are, as they are: mostly psychiatric terms
(psychotic disorder, auditory and visual hallucinations, delusion, paranoia, mania,
agitation, aggression, suicidal ideation, catatonia) and terms about use (treatment
noncompliance, loss of therapeutic response, therapy non-responder, drug screen positive,
product supply issue, adverse event). Xanomeline and trospium chloride is used for schizophrenia, so worsening or
unchanged illness is reported with it far more than with the average drug in FAERS:
reporting that follows the indication, not evidence of harm. MRD-201 studies the drug in
Alzheimer's disease; these reports come from another population.

**The FAERS caveats apply to every line above** (quoted in the method section).

### A mistake in the record, said plainly

The scan was started with `meridian signals scan --by karan.mehta`, so its
SignalScanRequested (seq 19985) records the medical monitor as having asked for it. He did
not: Claude Code started it while building this step. The default, `meridian.signals`,
was the right value. The ledger is append-only, so the event stays as it is; this note is
the correction. No decision was recorded on any signal.

## 1.0.1, pre-registered before its code and before it is run (phase 5 step 3b)

Written on 20 Nov 2026, after 1.0.0's 76 signals were seen (the table above): the lists
below were chosen knowing which terms 1.0.0 called signals, and that is said plainly
here. Nothing in the statistics or the signal rule changes.

### Retry

openFDA answers throttling either as HTTP 429 or, as it did four times in 1.0.0, as HTTP
500 with `"error": {"code": "SERVER_ERROR", "details": "Too Many Requests"}`. 1.0.1 retries
both, and nothing else, after 2, 4, 8 and 16 seconds (four retries, then the term is
reported as not answered, as before). Every attempt counts toward max_requests (900). Every
attempt's response is kept, so a replay sees the same answers.

### Rerunning on recorded responses

A scan may name an earlier scan whose responses it reuses (`reuses_responses_of` on
SignalScanRequested). The worker then requests only the queries of the plan that the
earlier scan has no 200 answer for, with the retry above, and runs on the earlier
answers plus the new ones. Every new answer must carry the earlier scan's data date
(`meta.last_updated`); if openFDA's data has moved on, the run fails and a full scan is
needed. The 1.0.1 rerun reuses scan seq 19985 (data date 2026-10-28; still openFDA's date
on 20 Nov 2026) and asks again for DRUG INTERACTION, HICCUPS, HYPERSEXUALITY and
INABILITY TO AFFORD MEDICATION.

### Context, as evidence on every signal

The agent's recorded context names the product by its brand, as the label does; it is
part of the agent's output and stays as recorded. In summary, with each quote cited:

1. FAERS reports here concern oral xanomeline and trospium chloride. "Trospium chloride is a
   muscarinic antagonist." (US label for COBENFY, openFDA set id 8f0e73bf-6025-44f6-ab64-0983322de0df, version 6, effective 2026-01-16, section 12.1.) The
   label lists "anticholinergic adverse reactions (including dry mouth, constipation,
   dyspepsia, urinary tract infection, and urinary retention)" (same label, section 5.8).
   Anticholinergic terms are likely explained by trospium.
2. The combination is "indicated for the treatment of schizophrenia in adults" (same label,
   section 1). MRD-201 is
   transdermal xanomeline alone in mild to moderate Alzheimer's disease
   (config/portfolio.toml). Route, combination and population all differ.
3. On an indication term (below): reported with a drug for schizophrenia, it is confounded
   by indication, the illness being treated rather than an effect of the drug.

### Triage (our choice)

Every signal by the unchanged rule gets one tier, the first that applies:

| Tier | Rule | In the review queue |
|---|---|---|
| listed | named in the label text (exact phrase, after the spelling rules below) | no |
| indication | an indication term | no |
| use | a term about use, effect or supply, not a clinical event | no |
| weak | lower 95% confidence limit of the PRR at most 2, or fewer than 10 reports | no |
| priority | everything else: lower limit above 2, at least 10 reports | **yes, as a finding** |

Only priority signals become findings (kind reporting_signal, decided by the medical
monitor). The other tiers stay on the Safety signals view, with their tier.

**Indication terms** (schizophrenia, its spectrum, and DSM-5 criterion A symptoms:
delusions, hallucinations, disorganized speech or thinking, disorganized or catatonic
behaviour, negative symptoms), as MedDRA preferred terms: SCHIZOPHRENIA, SCHIZOAFFECTIVE
DISORDER, PSYCHOTIC DISORDER, PSYCHIATRIC DECOMPENSATION, PSYCHIATRIC SYMPTOM, DELUSION,
PARANOIA, HALLUCINATION, HALLUCINATION, AUDITORY, HALLUCINATION, VISUAL, THINKING
ABNORMAL, TACHYPHRENIA, SOLILOQUY, CATATONIA, BLUNTED AFFECT, INAPPROPRIATE AFFECT, APATHY,
NEGATIVE THOUGHTS, INTRUSIVE THOUGHTS. Source for the symptom domains: American
Psychiatric Association, DSM-5 (2013), Schizophrenia, criterion A. The label also names
hallucinations among trospium's anticholinergic CNS effects (5.9); these terms stay visible
as indication-tier, not hidden.

**Use terms** (our choice): TREATMENT NONCOMPLIANCE, LOSS OF THERAPEUTIC RESPONSE, THERAPY
NON-RESPONDER, DRUG INEFFECTIVE, ADVERSE EVENT, DRUG SCREEN POSITIVE, PRODUCT SUPPLY ISSUE,
INABILITY TO AFFORD MEDICATION, DRUG INTERACTION.

**Spelling rules for the label comparison** (MedDRA uses British spelling, the label
American): "oesophag" as "esophag", "haem" as "hem", "oedema" as "edema", "diarrhoea" as
"diarrhea", "behaviour" as "behavior"; and one synonym the label itself uses in 6.1:
GASTROOESOPHAGEAL REFLUX DISEASE as "gastrointestinal reflux disease".

### Expected

From 1.0.0's table, with these rules and before the rerun: priority are VOMITING
PROJECTILE, MANIA, HYPERHIDROSIS, GASTROINTESTINAL DISORDER, SEDATION, ANGER, HYPERSOMNIA,
BRAIN FOG and COLD SWEAT (9), plus any of the four retried terms that qualify. 1.0.0's 76
findings leave the queue by the existing per-scan rule: superseded where 1.0.1 raises the
same term on the new scan, retired otherwise.

## 1.0.1, as it came out (20 Nov 2026)

Image from `1ac4bb8` (clean). portfolio_signal conformance passed (twice; the second run
was started by mistake). `meridian signals scan --reuse 19985`: SignalScanRequested seq
20105, scan SCAN-20261119T223555Z (the host clock's UTC), requested by operator:himanshu,
in the OpenShell sandbox.

- 4 queries asked again (DRUG INTERACTION, HICCUPS, HYPERSEXUALITY, INABILITY TO AFFORD
  MEDICATION): each answered 200 on the first try, data date 2026-10-28. The retry was
  not needed live; it is tested only on a fake.
- 258 responses (254 reused, 4 new); **250 terms tested, none unanswered; 78 signals**
  (76 as before, plus HICCUPS, a = 4, PRR 3.71, and HYPERSEXUALITY, a = 4, PRR 16.7, both
  weak). DRUG INTERACTION (PRR 0.38) and INABILITY TO AFFORD MEDICATION (chi-squared 3.7)
  are not signals.
- Tiers: listed 20 (GASTROOESOPHAGEAL REFLUX DISEASE now matches the label's
  "gastrointestinal reflux disease"), indication 18, use 6, weak 25, **priority 9**:
  exactly the 9 pre-registered.

| Priority signal | a | PRR (95% CI) | ROR | Chi-squared |
|---|---:|---|---:|---:|
| vomiting projectile | 10 | 26.37 (14.19 to 49.01) | 26.46 | 218.6 |
| mania | 23 | 10.64 (7.08 to 15.99) | 10.72 | 191.3 |
| brain fog | 12 | 5.82 (3.31 to 10.24) | 5.84 | 43.2 |
| sedation | 20 | 5.81 (3.75 to 8.99) | 5.84 | 74.9 |
| hyperhidrosis | 97 | 5.73 (4.71 to 6.96) | 5.90 | 375.8 |
| gastrointestinal disorder | 62 | 5.26 (4.12 to 6.73) | 5.36 | 210.7 |
| cold sweat | 12 | 5.18 (2.94 to 9.11) | 5.19 | 36.4 |
| anger | 18 | 4.05 (2.55 to 6.42) | 4.07 | 38.4 |
| hypersomnia | 15 | 3.98 (2.40 to 6.59) | 4.00 | 30.6 |

- `meridian signals replay --trigger 20105`: 9 drafts, sha256 `93f697211ed77615`,
  identical.
- 1.0.0's 76 findings: 9 superseded by 1.0.1's on the new scan, 67 retired. The medical
  monitor's review queue: **76 before, 9 after** (all reporting signals). The portfolio
  lead's: 175 before, 108 after.

Not every priority signal is new to a reader of the label. Sedation is close to the
label's somnolence, and vomiting projectile to vomiting. The exact-phrase rule (with
spellings and one synonym) does not equate them; the medical monitor will see both side
by side.

# preclinical.ind_readiness: pre-registration and evaluation

Phase 5 step 2. **Everything above "Results" was committed before any code of the agent
existed and before it ran on any inventory** (`git log --follow` on this file): the
requirements list (config/ind_requirements.toml), MRD-101's inventory and its generator,
the rules below and the stress set with its ideals. Background: docs/fih-dose-notes.md.

## What the agent does and does not say

- It compares MRD-101's nonclinical study inventory with a committed list of the studies
  ICH M3(R2) (and S7A, S7B and S2(R1), where M3(R2) points to them) expects before a
  first-in-human trial. Every requirement quotes its guidance and section; none is added
  that cannot be cited.
- It says a requirement is **met**, **not met** (with one finding per gap), or **not
  applicable** (a requirement for multiple-dose trials when the planned trial gives one
  dose). It never says the programme is ready for submission: that is a regulatory
  judgement the people of the programme make.
- A gap is a finding for this fictional asset, not an error. The inventory has some on
  purpose (below).

## Inputs

- **The inventory** (`data/inventory/MRD-101/inventory.csv`, committed; written by
  `meridian.preclinical.inventory`, seed 20261119): one row per study with study_id, title,
  study_type, species, route, duration_weeks, glp (Y, N, or empty when not recorded),
  report_status (planned, ongoing, draft, final, qa_audited), start_date,
  report_final_date and qa_audit_date (actual once done, planned before), and part_of (the
  study a toxicokinetic part belongs to). `meridian preclinical inventory-load` writes it
  as a parquet table to `data/inventory_cuts/MRD-101/<cut>/inventory/studies.parquet`
  (cut `INV-<first 8 hex of the CSV's sha256>`) and records an `InventoryLoaded` event,
  the agent's trigger. It is staged as `/stage/inventory`, which only the preclinical
  zone's policy reads.
- **The requirements** (config/ind_requirements.toml), **the planned trial** and the
  **planned submission date** (`[agents.ind_readiness]`): MRD-110, single and multiple
  ascending doses in healthy volunteers, so a multiple-dose trial of at most 14 days of
  dosing; IND submission planned for 2027-04-01 (the start of the portfolio's "Q2 2027").

## Rules (pre-registered)

**Species classes:** rodent RAT, MOUSE; nonrodent DOG, MONKEY, MINIPIG; in vitro IN VITRO
(`[classes]` in the requirements file). A requirement with species class "any" takes a
study of any species.

**Required duration** (M3(R2) Table 1, for the planned clinical duration d in days): d of
14 or less: 2 weeks (rodent and nonrodent); 14 < d <= 182: d / 7 weeks; d > 182: 26 weeks
in a rodent, 39 in a nonrodent. Footnote a (in the US, a clinical study shorter than 14
days may be supported by toxicity studies of the same duration) is not used: 2 weeks is the
minimum here.

**Candidates.** A study is a candidate for a requirement when its study_type is one of
the requirement's and its species is in the requirement's class. Each candidate has up to
three problems:
- **duration short**: the requirement takes its duration from Table 1 and the study's
  duration_weeks is missing or below the required duration;
- **report not ready**: the report is not QA audited by the planned submission date, that
  is, report_status is not qa_audited and qa_audit_date is missing or later than the
  submission date (a planned audit on or before it counts);
- **GLP status missing**: glp is empty. A study recorded as not GLP (N) is shown as such
  in the checklist and is not a finding (S7A 2.11 and S7B 1.4 allow a justified non-GLP
  study; M3(R2) does not require GLP in the text these requirements quote).

The requirement's **study** is the candidate with the fewest problems; ties go to the
longer duration, then to the earlier study_id. The requirement is **met** when that study
has no problem.

**Findings (checks), one per gap, on the chosen study only:**

| Check | When | Severity |
|---|---|---|
| `required_study_missing:<requirement>` | no candidate at all | high |
| `duration_short:<requirement>:<study>` | the study's duration is short | medium |
| `report_not_ready:<requirement>:<study>` | its report is not ready by the submission date | medium |
| `glp_status_missing:<requirement>:<study>` | its GLP status is not recorded | medium |

A requirement with applies = "multiple_dose" is **not applicable** when the planned trial
gives a single dose; it raises nothing and is left out of the percentage.

**Summary metric (not a finding):** percent of applicable requirements met. The API
computes it from the same inventory and rules (the agent's own functions), so the page and
the Portfolio show it live.

**Supersession.** A new inventory (a new CSV, so a new cut) gives a new run; a finding of
the same check on a newer cut supersedes the older one, and a gap the newer cut no longer
has is retired, as for the other agents.

## Expected on MRD-101's inventory (stated before the agent exists)

11 requirements, all applicable (a multiple-dose trial): 7 met, 63.6%. Gaps:
- `required_study_missing:in_vitro_metabolism_protein_binding`: none is planned;
- `report_not_ready:repeat_dose_nonrodent:MRD-101-TOX-02`: the dog study is in life and its
  QA audit is planned after 2027-04-01;
- `report_not_ready:toxicokinetics_nonrodent:MRD-101-TOX-02-TK`: the same, for its TK;
- `glp_status_missing:safety_pharmacology_respiratory:MRD-101-SP-03`.

Met through the best candidate: the rodent requirement by MRD-101-TOX-01 (13 weeks, GLP,
QA audited), not by the 4-week, non-GLP range-finder MRD-101-DRF-01; the nonrodent one is
not met although the 2-week dog MTD study MRD-101-DRF-02 is long enough, because neither
dog study is QA audited by the submission date (TOX-02 wins the tie on duration).

## Stress set (hand-written, committed with this file)

`tests/fixtures/ind_stress/*.json`: 12 cases, each a base (the committed inventory, or
empty), changes to it, the planned trial if it differs, and the ideal findings and
percentage (to one decimal) under `ideal["1.0.0"]`. The base "complete" is the committed
inventory with every gap filled (`tests/fixtures/ind_stress/_complete.json`: a metabolism
and protein binding study added, the dog study and its TK QA audited on 2027-03-15, the
respiratory study's GLP status recorded). Run once, at the end (`scripts/ind_stress.py`). Like
the tox stress set it tests the specification above, by the same author as the agent:
agreement shows the code does what the rules say, not that the rules are what a
regulatory reviewer would decide.

| Case | What it changes | Ideal |
|---|---|---|
| complete | every gap of the real inventory filled | nothing, 100% |
| real_inventory | nothing | the four gaps above, 63.6% |
| no_nonrodent | both dog studies and their TK removed | nonrodent repeat-dose and TK missing |
| rodent_too_short | only a 4-week rat study, trial of 6 weeks | rodent duration short |
| long_trial | trial of 9 months | rodent and nonrodent duration short (26 and 39 weeks) |
| draft_report_late | the hERG report a draft, audit after submission | hERG report not ready |
| audit_planned_in_time | a draft whose audit is planned before submission | nothing |
| glp_missing | the Ames study's GLP blank | Ames GLP status missing |
| single_dose_trial | single dose, no chromosomal test | chromosomal not applicable, nothing raised |
| best_candidate | a short and a long rat study | the long one is chosen, nothing raised |
| empty_inventory | no study at all | 11 missing, 0% |
| two_week_trial | trial of 14 days, a 2-week GLP dog study QA audited | nonrodent met by it |

## Generated faults (regression, not evaluation)

`meridian.preclinical.ind_cases` (`make_fault`): from the complete inventory, a seeded fault per
copy (drop a study, shorten a repeat-dose study below the required duration, move a QA
audit past the submission date, blank a GLP status), each with the one check it must
raise. A test runs 40 seeds and requires exactly the expected finding on each, nothing
else. They protect the code against regressions; they were written by the same author and
measure nothing about the rules.

## Sources

Read in full for this step (the FDA editions; PDFs not committed, sha256 of each):

| Document | sha256 |
|---|---|
| ICH M3(R2), FDA guidance, January 2010 (fda.gov/media/71542) | `f0ee0a86af2f0e58...` |
| ICH S7A, FDA guidance, April 2001 (fda.gov/media/72033) | `ea467eb708f40d61...` |
| ICH S7B, FDA guidance, October 2005 (fda.gov/media/72043) | `3d35e13ffe2b776a...` |
| ICH S2(R1), FDA guidance, June 2012 (fda.gov/media/71980) | `ac26e35d32e57c71...` |
| FDA, Content and Format of INDs for Phase 1 Studies, November 1995 (fda.gov/media/71203) | `70098f089447bace...` |

Every quote in config/ind_requirements.toml was checked against the text of its document
(whitespace normalised) when it was written.

**What the 1995 guidance says about the report check.** Section III.G.2 allows an IND's
integrated toxicology summary to rest on "unaudited draft toxicologic reports", with the
final, fully quality-assured reports available "within 120 days of the start of the human
study". So a report that is not QA audited by the submission date does not by itself block
an IND; `report_not_ready` measures the programme against its own plan (a QA-audited report
for every pivotal study at submission), not against a regulatory minimum.

## Results

Run on 20 Nov 2026, preclinical.ind_readiness 1.0.0 at commit `87256d0`; the
pre-registration (`cf72d99`) unchanged.

### Stress set (12 cases, run once): 12 as ideal

`scripts/ind_stress.py`, actuals written into each fixture under `actual["1.0.0"]`: every
case gives exactly its ideal findings and percentage.

**Said plainly:** before that formal run, two of its inputs had already been run by unit
tests written with the agent: the real inventory (the ideal of `real_inventory`, as
pre-registered above) and the complete base (the starting point of every generated
fault). Both matched; no rule or ideal was changed. The other ten cases were first run in
the formal run.

### Generated faults

40 seeds, every kind generated (drop, shorten, delay_report, blank_glp); each raises
exactly its expected finding and nothing else. The module is
`meridian.preclinical.ind_cases`, not `inventory_faults` as written above: it also builds
the stress cases.

### The real inventory, in a Substrate sandbox on kind

The image built from `0b22edd` (clean), templates `*-638a520b14fa-m384` deployed with
`meridian substrate deploy --prune`; preclinical zone conformance on Substrate passed
(every egress refused, controls allowed). `meridian preclinical inventory-load` recorded
the committed inventory as cut `INV-4ced19d3` (InventoryLoaded, seq 19956); one run in an
actor of `preclinical-638a520b14fa-m384`, 0 failed, drafts identical to an in-process run
of the same trigger (drafts sha256 `dc25595d...`). **As pre-registered:** 7 of 11
requirements met (63.6%), four findings:

| Finding | Seq | Gap |
|---|---:|---|
| PRE-0233 | 19961 | `report_not_ready:repeat_dose_nonrodent:MRD-101-TOX-02` (QA audit planned 2027-06-16) |
| PRE-0234 | 19962 | `glp_status_missing:safety_pharmacology_respiratory:MRD-101-SP-03` |
| PRE-0235 | 19963 | `report_not_ready:toxicokinetics_nonrodent:MRD-101-TOX-02-TK` (2027-06-13) |
| PRE-0236 | 19964 | `required_study_missing:in_vitro_metabolism_protein_binding` |

In the same pass preclinical.dose_translation ran on the latest NOAEL decision (seq 19943)
in the same template: PRE-0232 (seq 19958), a proposed starting dose of 1.94 mg for a
60 kg adult from the combined NOAEL of 2 mg/kg (docs/fih-dose-notes.md), drafts identical
to an in-process run (`c1728dfe...`). It is open: no decision was recorded on it. After the
pass `meridian ledger verify` is ok at 19,965 events.

### Correction (phase 5 step 3a)

The pre-registration above says the planned trial is "MRD-110, single and multiple
ascending doses". That was wrong: MRD-110 is a separate asset already in phase 1; the
trial these requirements serve is MRD-101's own first-in-human study. No design for it is
recorded anywhere, so the same assumption is kept (single and multiple ascending doses, at
most 14 days of dosing, multiple doses). Nothing in the rules, the inventory or the
results changes; the text above is left as it was committed.

### Leak check with the step 2 inputs (phase 5 step 3a)

`meridian leakcheck --env kind --zone preclinical` now also runs dose_translation and
ind_readiness, with the canary planted in their staged inputs: the NOAEL extract
(`recorded_at`) and a copy of the inventory cut (`route`), columns the agents read but
never put in a finding, besides MI's MIORRES in the SEND copy. Run on 19 Nov 2026 (UTC),
20 runs (7 tox_study_monitor, 7 dose_translation, 6 ind_readiness), the step 2 image:
**pass**, the canary in none of the six sources, every positive control found
(`var/leakcheck/kind-20261119T202520Z.json`):

| Source | Items | Canary | Positive control |
|---|---:|---|---|
| dispatcher input store | 17 | none after the drop | the NOAEL extract, the inventory and MI held during the pass |
| dispatcher pod | 11,269 | none | marker in its /tmp |
| dispatcher logs | 1 | none | marker logged as a request path |
| snapshot bucket | 48 | none | each of 6 golden snapshots decodes to the worker's path |
| node kind-control-plane | 103,050 (15.4 GB) | none | marker in the scan pod's layer |
| host var/ | 118 | none | marker file |

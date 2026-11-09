# SAE reconciliation: notes

## Part 0: the data (curated DC-BASE, blinded AE)

| Measure | Count |
|---|---|
| AE rows | 1191 |
| Rows with AESER = Y | 3 |
| Subjects with a serious AE | 3 (201-1009-023, 201-1018-006, 201-1018-012) |
| Sites involved | 2 (1009, 1018) |
| AESDTH, AESDISAB, AESCONG | present; N on all 3 serious rows |
| AESLIFE | present; Y on 1, N on 2 |
| AESHOSP | present; Y on 2, N on 1 |
| AESMIE | absent from the pilot |
| AEOUT (serious rows) | RECOVERED/RESOLVED on all 3 |
| AEREL (serious rows) | POSSIBLE 1, PROBABLE 1, NONE 1 |
| AESTDTC precision, all rows | date 1165, year and month 15, year only 11; no date-times |
| AESTDTC precision, serious rows | date on all 3 |

Three serious AEs is enough to continue (the stop rule was fewer than 3), and too few to
evaluate on, which is why the evaluation uses seeded cuts that promote extra AEs to serious
(docs/safety-eval.md). The serious terms are syncope (twice) and partial seizures with
secondary generalisation.

## What SAE reconciliation is, and why sponsors do it

A serious adverse event is recorded twice. The site enters it in the clinical database
(the eCRF, SDTM AE) and also reports it, separately and fast, to the sponsor's safety
database, which feeds expedited regulatory reports and the safety profile of the drug.
The two systems are built by different teams, for different purposes, on different
timelines. Reconciliation compares them case by case: every serious AE in the clinical
database should have one case in the safety database, with the same event, onset,
outcome and causality, and every case should have its serious AE.

A mismatch matters either way. A serious AE with no case may never have been assessed for
expedited reporting. A case with no serious AE (or one the site later downgraded to
non-serious) makes the clinical data understate risk. Differing outcomes or causality mean
one of the two records is wrong, and the regulator may see both.

## The 24 hour expectation, and late reporting

Under ICH E6 and the protocol, a site reports a serious AE to the sponsor immediately,
which protocols translate into within 24 hours of the site becoming aware of it. The
sponsor's own clock (for example 7 or 15 days to report a SUSAR to regulators) starts
when the sponsor receives the case, so a late site report eats into it. One late report is
a deviation to follow up; a site that reports most of its serious AEs late is a training or
oversight problem, which is why there is a site-level finding as well (2 or more late cases
at one site). Here, awareness and receipt are synthetic date-times; receipt more than 24
hours after awareness is late.

## Why the safety database may hold unblinded data, and how it stays out

To report a suspected unexpected serious adverse reaction (SUSAR) to regulators, the
sponsor's safety team unblinds that one case: the expedited report states the treatment.
So a real safety database holds treatment for some cases while the trial is still blinded.

In this design the generator writes that treatment (column TRTA) only for a few related
cases. Ingestion splits it off exactly as it splits DM's treatment columns: the blinded
case table (blinded/safety_cases.parquet) has no treatment column, and the cut writer
refuses to write any column whose name contains TRTA (or ARM, ACTARM, ...) to a blinded or
ops area before writing a byte. The treatment goes to unblinded/safety_unblinded.parquet
with the case id. The agent's sandbox is staged with the blinded case table only; the
stage check refuses any unblinded path. The API serves the unblinded statistician a count
of such cases and never the value.

## Why subject-level findings never go to the cloud model

A reconciliation finding is about one subject: its title, summary and evidence carry the
subject id, the event, its dates and the case id. That is patient-level data, and the
narrator sends what it narrates to a cloud model through the OpenShell provider. So the
narrator's subscription excludes the safety zone outright (not just by agent version),
the exclusion is checked again before each request is built, and a test shows that a
safety finding is never pending, never narrated and never sent, even if someone adds the
agent to the narrator's subscriptions. The reconciler's own sandbox policy has no network
rule and no provider. Patient-level findings are narrated by safety.local_narrator with
the local model (phase 3 step 3b, docs/local-model-notes.md).

Visibility is also per finding. Subject-level findings reach medical_monitor,
clinical_ops_lead and qa_auditor only; the site-level late reporting finding carries no
subject id and may also reach portfolio_lead. The rule lives in the agent's config
(`[agents.sae_reconciler]`); the runner refuses a draft that names a role outside the
agent's own. The API ledger redacts any event about a finding the caller may not see, so
a subject id does not leak through the event list either.

## The matching rule

1. Exact: same subject, same preferred term after normalising (upper case, whitespace
   collapsed), same onset day (a date-time and a date on the same day agree).
2. Fallback, on what is left: same subject and preferred term, onsets at most 3 days
   apart, closest pair first. A fallback pair is one record with an onset mismatch, not
   two missing records.
3. Each pair: onsets more than 1 day apart, a different outcome or a different causality
   is a field mismatch (medium). A clinical SAE left over has no case, a case left over has
   no clinical SAE (high); the evidence says whether the clinical AE exists but is recorded
   non-serious. A case received more than 24 hours after awareness is late (high).

Known failure modes:

- **Onsets more than 3 days apart** read as a missing case plus an orphan case, not as one
  onset mismatch. A test pins this down.
- **Partial clinical onsets** (YYYY or YYYY-MM, 26 of the base AE rows, none serious
  today) have no day, so they never match.
- **A different preferred term** (recoded in one system, or a different MedDRA version) means
  no match: the reconciler does not compare verbatim terms or use the MedDRA hierarchy.
- **Two serious AEs with the same subject and preferred term within the window** can be
  paired the wrong way round. The evaluation never creates them, so this is not measured.
- **Seriousness criteria** (death, hospitalisation, ...) are carried on each case but not
  compared.
- **Case versions and follow-ups** are not modelled: the safety database here holds one
  version per case.

## The matching rule, 1.1

(1.1.1, phase 3 step 3a polish, changes text and evidence only: a late report gives its
delay as "126 hours (5.2 days)" in title, summary and evidence, and its evidence names the
case and, when the case pairs with a clinical SAE, that record by USUBJID and AESEQ. It
raises the same findings as 1.1.0 on every record; `MATCHING_VERSION` stays 1.1.0, and the
stress sets' recorded outcomes are keyed by it.)

safety.sae_reconciler 1.1.0 replaces the rule above. It was built from the development
stress set's misses (docs/safety-eval.md). The full rule is in the agent's docstring; in
short:

1. **Records.** Every input is sorted by its full content first, so the result never
   depends on export row order. Safety rows are grouped by case id. The latest version (by
   receipt, then CASE_VERSION) is the case; earlier versions are history. Timeliness is
   measured on the initial version, the one the 24-hour rule is about.
2. **Data quality.** A record missing a field it needs for matching (subject, AESEQ,
   preferred term, an onset that is not even a year) gets sae_data_quality (medium) and is
   left out of matching. A field needed by one check only (awareness, receipt, an outcome,
   causality or criteria flag outside the terminology) gets sae_data_quality, and that one
   check is skipped. Either way the rest of the cut is reconciled.
3. **Onsets** are intervals: a day, a month (YYYY-MM) or a year (YYYY). The gap is the
   number of days between the intervals, 0 when they overlap.
4. **Pairing**, per subject and preferred term, is an optimal assignment. Pairs are allowed
   within fallback_window_days (default 7). The total is minimised, where
   `cost(pair) = 1 x gap days + 3 x [outcome differs] + 1 x [causality differs]` and every
   unpaired record costs more than any allowed pair. Ties go to the lexicographically
   smallest assignment.
5. **Left over**, in order:
   - **Subject typo:** a case with an unenrolled subject and exactly one enrolled subject at
     its site within edit distance 1 (a transposition counts as 1) that has an unpaired SAE
     with the same term within the window gives sae_subject_mismatch (high). Two or more
     candidates give no guess.
   - **Recoded term:** a subject's only unpaired SAE and only unpaired case, with different
     terms and onsets within 1 day, give sae_term_mismatch (medium). This is a
     deterministic stand-in: whether two terms mean the same event is a judgement for the
     local model in step 3b, not this step.
   - **Probable duplicate:** an unpaired case with a same-subject, same-term case within the
     window gives sae_probable_duplicate (medium).
   - What is still left is a missing case or a case without an SAE, as before.
6. **Per pair:**
   - **Onset:** a gap above 1 day is an onset mismatch. A pair through a partial onset that
     agrees within the tolerance gives an sae_partial_date note (low) instead.
   - **Outcome and causality** are compared after mapping through config/safety_terms.toml.
     When either causality side is binary (RELATED, NOT RELATED), the two are compared by
     class.
   - **Seriousness criteria:** the six flags are compared where both sides record them
     (seriousness_criteria_mismatch, medium). The pilot has no AESMIE, so that flag is
     never compared on its data.
7. **Evidence** names the clinical record by USUBJID and AESEQ, and the safety record by
   case id, version and USUBJID.

Known limits of 1.1:

- An excluded record can leave its counterpart as a missing case or an orphan.
- Groups of more than 12 cases for one subject and term are paired greedily.
- A subject typo is only recognised when the term also matches.
- The recoded-term rule pairs on dates, not meaning.

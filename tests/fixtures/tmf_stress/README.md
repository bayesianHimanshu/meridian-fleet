# TMF completeness stress set

Hand-written eTMF index rows, monitoring visits and DM site dates, written before any agent
code of quality.tmf_completeness existed (step 4a). Not produced by the eTMF generator
(meridian.tmf) and not built with the agent's normalisation: every value is written out
as an eTMF export might hold it, with readable document ids.

`_base.json` is a small clean trial: sites 9001 and 9002, three subjects, every expected
artifact of config/tmf_expected.toml filed on time with QC passed, export date
30 Jun 2014. Each case file changes it:

- `dm_set` {USUBJID: {field: value}}, `visits_add`, `visits_remove` (VISIT_ID),
  `index_add`, `index_remove` (DOCUMENT_ID), `index_set` {DOCUMENT_ID: {field: value}};
- `case`, `description`: what the case is about;
- `ideal`: what a TMF reviewer would want reported, in words;
- `ideal_findings`: the same as `check:record` (record: DOCUMENT_ID, VISIT_ID,
  `<site>/<artifact key>` or `TRIAL/<artifact key>`, and `<DOCUMENT_ID>#<FIELD>` for
  data quality);
- `actual`: per agent version, what the agent reported when the set was run once at the
  end of step 4a (docs/tmf-eval.md). Empty until then.

The agent was not changed after that run. Gaps between ideal and actual are the backlog
for the next version.

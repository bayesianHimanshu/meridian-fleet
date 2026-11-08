# SAE reconciliation stress set

Hand-written clinical AE rows and safety cases, one file per case. Not produced by the
safety database generator (meridian.safetydb) and not built with its normalisation code
(meridian.domain.safety): every value below is written out as a site or a safety system
might hold it.

Each file has:

- `case`, `description`: what the pair is about;
- `dm`: subject and site;
- `ae`: SDTM AE rows (seriousness criteria AESDTH ... AESCONG default to "N" when absent);
- `safety_cases`: rows of the blinded case table (SDTH ... SCONG default to "N");
- `ideal`: what a safety reviewer would want reported;
- `predicted`: what safety.sae_reconciler 1.0.0 should report under its current rule,
  as `check:record` (record: subject/AESEQ or case id, as in the dedup key), or
  `run fails` if the agent should raise; a trailing `?` marks a finding that may or may
  not appear (order not defined). Written before the first run.
- `actual`: added after the run (docs/safety-eval.md, "Stress set").

`tests/unit/test_sae_stress.py` runs each one through the agent and checks the result
against `actual`, so a change in behaviour shows up; it does not check against `ideal`.
Gaps between ideal and actual are the backlog for the next agent version.

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
- `ideal_findings` (added for 1.1): the ideal as `check:record` in the 1.1 check
  vocabulary. The words in `ideal` came first; this only restates them;
- `actual`: per agent version, the outcome recorded after that version's run
  (docs/safety-eval.md).

`tests/unit/test_sae_stress.py` runs each one through the agent and checks the result
against `actual` for the current version, so a change in behaviour shows up; it does not
check against the ideal.
Gaps between ideal and actual are the backlog for the next agent version.

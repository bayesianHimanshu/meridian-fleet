# Audit trail stress set

Small audit extracts written by hand for step 4b, before any code of
quality.audit_trail_reviewer existed. They are the rows the agent reads
(meridian.domain.audit), not real ledgers, and were not produced by the exporter
(meridian.audit.extract) or by any generator of the project. Each value was chosen by hand;
a throwaway helper only typed the repeated row shapes (a DataCut, a run, a decision).

Each file has:

- `case`, `description`: what the case is about;
- `through_seq`: the last seq of the review (the trigger's);
- `rows`: extract rows; columns left out are empty. Two shorthands, applied by
  tests/audit_stress_support.py and nothing else: a row without `HASH` has hash "h<SEQ>",
  and a value "h<n>" stands for the 64-hex string of n (h3 = 000...003); a row without
  `PREV_HASH` links to the previous row's hash (the first to the genesis hash, 64 zeros);
- `ideal`: what an auditor would want reported, in words;
- `ideal_findings`: the same as `check:record`, with these records: `gap-<expected>-<found>`
  and `link-<seq>` (chain linkage), `<agent>/<version>/<trigger seq>` (unsandboxed run),
  `<agent>/<version>/<trigger seq>/<woke seq>` (failed run), `<actor>/<first seq>` (rapid
  decisions), `<decision seq>` or `<actor>/reused/<first seq>` (reason quality),
  `<decision seq>` (decision on a retired or superseded finding);
- `actual`: per agent version, what the agent reported when the set was run once at the end
  of step 4b (1.0.0) and once in phase 4a step 2 (1.1.0) (docs/audit-eval.md). Empty until
  then.

Phase 4a step 2 added two cases, 17_clean_substrate_runs and
18_substrate_run_records_incomplete, written by hand with their ideals before 1.1.0 was run
on them: 1.1.0 accepts substrate as a sandboxed executor when its completion records the
image, the template spec hash and the golden snapshot id.

Thresholds assumed by the ideals (the agent's documented defaults): 10 decisions within 5
minutes (inclusive) by one actor is rapid; a dismissal reason shorter than 12 characters or
a placeholder is poor; a dismissal reason identical to at least 3 others by the same actor
is reused.

These ideals were written by the same model that then writes the agent. They test that the
agent implements this specification, not how it behaves on cases nobody wrote down.

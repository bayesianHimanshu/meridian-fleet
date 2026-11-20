# SAE reconciliation stress set: sealed holdout

16 hand-written cases (4 controls). They were written for safety.sae_reconciler 1.1 before
any 1.1 code, and the agent is not run on them before the single judging run
(docs/safety-eval.md, "Sealed holdout"). Every value was chosen by hand. A short helper
script only wrote the JSON. Nothing comes from the generator or the agent's normalisation
code.

Same format as tests/fixtures/sae_stress/, with:

- `control`: true for clean controls;
- `ideal`: the findings a safety reviewer would want, as `check:record` in the 1.1 check
  vocabulary. Record keys are the clinical subject/AESEQ or the case id. For
  sae_data_quality the key is `<record>#<FIELD>`, and for sae_probable_duplicate it is the
  two case ids joined by `+`, sorted;
- `ideal_note`: the ideal in words.

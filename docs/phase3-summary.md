# Phase 3 summary: review, safety, a local model, TMF and the audit trail

Phase 3 put people in the loop and widened oversight beyond RBQM. Agents still raise
findings as pure functions of their inputs in OpenShell 0.1.7 sandboxes; people now read
and decide them through an API and a web client, and every decision is a ledger event.
The ledger closes the phase at 4,558 events, `meridian ledger verify` ok (phase 2 ended
at seq 786: phase 3 is seq 787 onwards).

| Step | PR | What it added |
| --- | --- | --- |
| 1 | 11 | Fleet API: development identity, one visibility module, decisions as events |
| 2 | 12 to 14 | Command Center (React), cut scope, superseded findings, 390 px layouts |
| 3a | 15 to 18 | safety.sae_reconciler 1.0 to 1.1.1, synthetic safety database, Safety page |
| 3b | 19, 20 | Local model (Ollama) for term adjudication and safety narration; pre-commit check |
| 4a | 21 | quality.tmf_completeness 1.0.0, synthetic eTMF, TMF page; narrator selector_leak |
| 4b | this | quality.audit_trail_reviewer 1.0.0, segregation of duties, QA observations |

## What was built, and the evidence

1. **Fleet API and Command Center.** Role-based visibility lives in one module
   (src/meridian/api/visibility.py); a hidden finding is 404, never 403. The two demo
   decisions are seq 787 and 788. A hypothesis property test asserts that no endpoint shows
   a role a string of a finding outside its visible_to; it found real leaks twice (ledger
   events about hidden findings in 3a, the QualityObservation event in 4b) and both were
   fixed by redaction, not by filtering in a handler.
2. **SAE reconciliation.** Deterministic matching of clinical SAEs against a generated safety
   database: findings 1.0.0 seq 867 to 1162, 1.1.0 seq 1792 to 2087, 1.1.1 seq 2159 to 3025;
   581 sandboxed run events, seq 864 to 3026. Operational cuts run only in openshell.
3. **A local model.** Ollama on the host, reachable only from the safety.local_model
   sandbox (GET /api/tags and POST /api/chat, python only). Term adjudication 1.1.0 asks
   phi4-mini and qwen3.5 and says yes or no only when both agree (assessments seq 3027 to
   3041); the local narrator explains safety findings with subject ids that never leave the
   machine (narratives seq 2555 to 2981).
4. **TMF completeness.** 16 artifacts by TMF Reference Model name (no numbers: none could
   be verified), site milestones from DM, a seeded eTMF index staged only for the quality
   zone; findings seq 3194 to 3460, 142 sandboxed run events seq 3191 to 3481.
5. **Audit trail review.** The host exports identifiers and hashes only; the reviewer runs
   under its own policy (meta and audit, no network). Review of seq 1 to 4548 at seq 4549 to
   4558; conformance of all 11 policies on the final image at seq 4431 to 4548.
6. **Segregation of duties.** qa_auditor never decides; audit findings are decided by
   clinical_ops_lead; nobody decides a finding naming them as the actor under review (403,
   tested on every finding kind); QA observations are raised by the auditor and decided by
   someone else.

## Evaluation, including the misses

| What | Result | Note |
|---|---|---|
| SAE reconciler, generated dev and holdout | precision and recall 1.0 | regression check by construction |
| SAE sealed stress set (1.1) | ideal met 8 of 12 (target 10) | missed; the four misses are its backlog |
| Term adjudicator 1.0.0, sealed term set | 26 of 33 (0.788, target 0.80), 0 wrong "yes" on look-alikes | missed accuracy |
| No model ever said "unsure" | 0 in 3b; all 14 dev pairs labelled unsure got "no" in 3b2 | abstention comes only from disagreement |
| Agreement 1.1.0 / 1.1.1, 75 dev pairs | 55 / 57 correct, 0 wrong "yes" | costs correct answers |
| Term path end to end | 15 assessments, 0 wrong "yes", 1 wrong "no" | |
| Local narrator 1.0.0 / 1.0.1 | 16 of 77 / 135 of 158 accepted | |
| Narrator semantic slips | a date given the wrong meaning, two records conflated, one factual error; 12 of 135 accepted summaries pasted selectors | the validator cannot see meaning; selector_leak now catches the pasting |
| TMF, generated dev and holdout | precision and recall 1.00 | regression check |
| TMF stress set | 15 of 15 | written to the same specification |
| Audit stress set | 16 of 16 | written to the same specification |
| Audit review of the real ledger | 7 findings, all phase 1 runs before sandboxes | true and historical |

## Known gaps

- **Stress sets written by the same model family as the code.** Every hand-written
  stress set in this phase (SAE, TMF, audit) was written by the model that then wrote the
  agent. They test the specification, not cases nobody thought of. Only the user's sealed
  sets (SAE holdout, term pairs) were independent, and both missed their targets.
- **Consent dates are missing in the pilot.** RFICDTC is empty for all 306 subjects, so "first
  subject" is the first treatment; an approval between consent and first dose is missed.
- **Misfiling needs a field real indexes lack.** The synthetic eTMF records the site a
  document names (DOC_SITE); a real check would read the document.
- **Any network rule widens readable paths on 0.1.7.** /usr, /lib, /etc, /var/log and /proc
  become readable in the narrator and local model sandboxes (docs/threat-model.md).
- **Answers are not repeatable at temperature 0 with a fixed seed.** Re-sending 7 rejected
  narrator requests changed 2 answers; a rerun of the dev pairs can move a pair.
- **Development identity only.** Anyone who reaches 127.0.0.1:8765 can claim any listed person.
- **The audit reviewer sees timings, not reading.** 787 and 788 were 47 ms apart and pass.

## Costs and timings

- Cloud model: no Anthropic call in phase 3 (all phase 3 narration is local); phase 2's
  narrator calls cost about $0.0094 in all.
- Local model on the host GPU: phi4-mini about 1.0 s per answer, qwen3.5 about 2.7 s,
  agreement about 3.7 s per pair; a first, cold call about 7 to 18 s.
- A sandboxed run costs about 8 s against tens of ms in process; background teardown keeps
  a pass close to the sum of its runs. 71 TMF runs took a few minutes per set.
- The pre-commit hook (scripts/check.sh) takes about 6 minutes; CI about 6.5 minutes for
  the checks job and 30 to 40 s for web.

## Ten lessons

1. **Keep visibility in one module and test it as a property.** Twice the property test
   found a leak the endpoint tests missed; both fixes were one function in one place.
2. **A generated evaluation is a regression check.** When the generator and the agent
   share rules, 1.00 measures consistency, not correctness; say so next to the number.
3. **Only an independent test set can disagree with you.** The sealed sets were the only
   evaluations that missed, which is why they were worth having.
4. **Write the ideal before the code, and admit who wrote it.** It catches gaps, but a
   stress set by the author's own hand tests the specification, nothing more.
5. **Small models do not abstain when asked.** No prompt made them say "unsure"; asking two
   models and abstaining on disagreement did.
6. **Validate structure, not meaning.** The validators catch numbers, dates, ids and
   pasted selectors; they cannot tell a correct sentence from a plausible wrong one.
7. **Let the host write what must be right.** The narrator lost 60 of 77 summaries to a
   missing site id until the host wrote the opening itself.
8. **Give each data area its own folder and policy.** /stage/tmf and /stage/audit each have
   exactly one reader, proved and shown by conformance, not just configured.
9. **Record what the reviewer needs, nothing more.** The audit extract replaces a check that
   may name a subject with a group number; the review loses nothing it uses.
10. **Segregate duties in the decision path itself.** Who may decide is one rule in the
    visibility module, checked on every decision, tested on every finding kind.

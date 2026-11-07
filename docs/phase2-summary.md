# Phase 2 summary: containment, and the first LLM use

Phase 2 moved the deterministic RBQM agents into NVIDIA OpenShell 0.1.7 sandboxes (Docker
driver), proved what those sandboxes can and cannot do, and added one LLM agent
(rbqm.narrator) whose only egress is the Anthropic Messages API, through an OpenShell
provider. Detection never changed: every finding is still a pure function of the cut.

| Step | PR | What it added |
| --- | --- | --- |
| 1 | 6 | One sandbox per run from the agent image, zone policies, meridian-worker; sandboxed drafts byte-identical to in-process ones. |
| 2a | 7 | Inputs manifest checked by the worker, background teardown with a tracker and `reap`, openshell-prover in CI. |
| 2b | 8 | `meridian sandbox conformance` per zone, with the supervisor's OCSF log lines in the ledger; docs/threat-model.md. |
| 3 | 9 | rbqm.narrator 1.0.0 behind the meridian-anthropic provider; host-side validation; FindingNarrative events. |
| 4 | 10 | Evidence context (role, direction, expected, display); detectors 2.2.0 / 2.1.0 / 2.1.0 with identical findings; narrator 1.1.0 and a labelled validator evaluation. |

## What containment proves, and how

Evidence is in the ledger (`var/ledger.sqlite3`, 786 events, `meridian ledger verify` ok)
and in tests that run in CI.

1. **Unblinded data never reaches a sandbox.** Staging copies only `blinded/`, `ops/`, the
   trigger and an inputs manifest; `assert_clean_stage` runs after staging and again just
   before upload, and the worker refuses any file not in the manifest (exit 3). Every
   sandboxed run records the manifest's sha256 in its ActorSuspended: 102 runs, seq 400 to
   770 (the step 4 re-run is seq 523 to 770). Tests: tests/unit/test_executor.py,
   tests/unit/test_inputs_manifest.py.
2. **Paths outside the allow-list are refused.** `meridian sandbox conformance` reads
   `/etc/os-release` and writes `/var/tmp` and `/opt/meridian` in a real sandbox per zone;
   each is refused with `Permission denied` naming the target, and a `/tmp` control
   succeeds. ContainmentCheckCompleted for all 7 zones at seq 422 to 452, PolicyDenied
   (`source = "harness"`) between them.
3. **There is no outbound network, except the narrator's one endpoint.** The same check
   connects to 192.0.2.1:443; the connect fails and the supervisor logs
   `NET:OPEN [MED] DENIED ... -> 192.0.2.1:443 [reason:transparent_tcp_policy_denied]`,
   stored verbatim (`source = "openshell_log"`, 44 PolicyDenied in seq 418 to 774). A
   missing line fails the check. The parser is pinned to real 0.1.7 lines.
4. **Policies stay inside their boundaries.** `meridian policy prove` runs openshell-prover
   on all 8 committed policies (7 zones within `blinded`, rbqm.narrator within `narrator`)
   in CI; a variant with another host, another binary, root, best-effort Landlock or an
   extra path fails.
5. **The narrator never holds the key.** The provider keeps `ANTHROPIC_API_KEY` on the
   gateway; the sandbox sees a placeholder that the proxy replaces only on
   `POST /v1/messages` (and the conformance GET) to api.anthropic.com:443 from
   `/usr/local/bin/python3.14`. Conformance for rbqm.narrator at seq 457, 462, 467 and 775:
   GET /v1/models 200 through the proxy, example.com and 192.0.2.1 refused and logged, and
   `credential_value_visible = false` (only true or false is ever recorded). The client
   refuses a key that looks real, and `meridian agents narrate` refuses to run unless the
   latest conformance run passed with the committed policy. meridian-worker's import
   closure has no network module; meridian-narrator-worker reaches the network only through
   the model client (tests/contract/test_worker_isolation.py).

## Costs and timings

* A sandboxed agent run costs about 8.1 s against 6 to 50 ms in process: create and upload
  1.5 s, worker 1.4 s (after precompiling bytecode; 3.0 s before), policy read 0.04 s,
  delete 5.4 s.
* Teardown after step 2a: background deletion cut the per-run critical path from 8.5 s to
  2.9 s and a 6-run pass from 25.5 s to 14.6 s. The step 4 re-run of 96 runs took 278 s,
  2.9 s per run.
* Narration: 1.0.0 on 53 findings, about $0.0076 (46,115 input, 6,035 output tokens);
  1.1.0 on 11 findings, about $0.0018 (11,881 and 1,264). About 1.2 s per call inside the
  sandbox, one sandbox per pass. claude-haiku-5-5 at $0.10 and $0.50 per million tokens.

## Known gaps

* **Landlock refusals have no OpenShell log record** on 0.1.7: file refusals are known only
  from the harness's own observation (exit code and stderr), not from an independent
  source. Backlog: kernel-level Landlock audit records.
* **Any network rule widens readable paths.** With a network rule, 0.1.7 adds `/usr`,
  `/lib`, `/etc`, `/var/log`, `/proc` and `/dev/urandom` to the read-only paths. The
  narrator sandbox holds only its job, so this exposes the image, not the trial; it is
  pinned by `expected_effective` and would show up as a policy mismatch if it changed.
* **The prover's filesystem check is allow-list matching only.** A candidate path must
  appear literally in the boundary; the prover does not reason about what a path contains
  or about prefixes. The render test and the conformance check carry the real weight.
* **Single-seed detection is noisy.** Each cut is one draw of the data, and the phase 1
  results move between draws: the same 70% AE drop left its target site at z = -1.6 to
  -1.9 on the dev cuts and -1.4 to -2.6 on the holdouts (all missed), and detector recall
  stayed at 2 of 3 faults per cut. One draw per cut cannot separate a real effect from a
  lucky or unlucky one; repeated seeds per scenario would.
* **The narrator's validator is strict where it is measured.** Validator 1.1.0 caught all
  5 known-bad narratives and all 240 mutations, but rejected 28 of 47 good 1.0.0
  narratives (rounded numbers, true citations of the summary). Labels are
  assistant-judged, and mutations share an author with the rules. The live 1.1.0 run
  accepted 10 of 11 but reads stiffly and copies statistical jargon from displays.

## Five lessons

1. **Prove containment in the running system, not in the policy file.** The effective
   policy differs from the file (comments dropped, a baseline added with any network rule,
   a provider rule added), uploads run under Landlock, and `touch` is refused before it
   tries a path. Each was found only by running a sandbox and reading what it reported.
2. **A check counts only for the right reason.** A refusal passes only when the error names
   the target; anything else is inconclusive. The same rule applied to the validator: a
   rejection counts as a catch only if it gives the expected reason, and RBQM-0010 is
   "caught" by a rounding rule, not by understanding its error.
3. **Give the model context instead of policing it afterwards.** Three of the five bad
   narratives came from evidence that lacked a role or a reference (a zero share near
   chance, a p value that underflowed). Describing evidence fixed them at the source; the
   validator only backs that up.
4. **Measure the guard, not just the guarded.** 53 of 53 accepted looked like success;
   a labelled set showed validator 1.0.0 caught none of the 5 bad ones, and 1.1.0 trades
   that for 28 false rejections. Targets were committed before the set was built, and one
   validator bug (a decimal point treated as a clause end) was found by it.
5. **Keep the secret out of the agent entirely.** The provider holds the key, the sandbox a
   placeholder, the client refuses anything that looks real, and the harness records only
   whether the in-sandbox value looks real. The only reads of `ANTHROPIC_API_KEY` in this
   repo are of that placeholder, inside the sandbox.

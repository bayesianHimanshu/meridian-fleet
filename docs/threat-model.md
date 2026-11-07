# Threat model: the blinding control (phase 2)

What we protect: unblinded data (treatment assignment, kept in `<cut>/unblinded/`), the
audit ledger, and any credential on the host. The agents that read study data run in
blinded zones; a buggy agent or a bad dependency in the agent image must not be able to
see unblinded data, change the ledger, or send data anywhere.

The control rests on three facts. Each has evidence a reviewer can rerun.

## 1. Unblinded data is never staged

Only `<cut>/blinded/*.parquet`, `<cut>/ops/*.parquet`, the trigger and the inputs manifest
are copied into the folder uploaded to a sandbox. The cut's own manifest (which lists the
unblinded files) and ground truth are never copied.

Evidence:
* `assert_clean_stage` in `src/meridian/sandbox/staging.py` runs after staging and again
  just before upload; anything else (an `unblinded` directory, a symlink, a stray file)
  stops the run.
* Tests: `test_staging_holds_only_blinded_ops_and_the_trigger`,
  `test_assert_clean_stage_refuses_anything_extra` and `test_a_symlink_in_the_cut_is_refused`
  in `tests/unit/test_executor.py`.

## 2. Paths outside the allow-list are refused

Each zone's committed policy lists the only paths a sandbox may read, and `/tmp` as the
only writable path. Landlock enforces it in the kernel (`hard_requirement`), so it holds
even for a process that ignores our code.

Evidence:
* `meridian sandbox conformance --zone all`: in a real sandbox per zone, reading
  `/etc/os-release` (a file in the image, not listed) and writing `/var/tmp/conformance`
  and `/opt/meridian/conformance` are each refused with `Permission denied` naming the
  target, while a control write to `/tmp` succeeds. Recorded in the ledger as PolicyDenied
  events with `source = "harness"` and one ContainmentCheckCompleted per zone.
* `tests/unit/test_policies.py`: the committed files equal their rendering and list only
  allowed paths; `meridian policy prove` checks them against the boundary in CI.

## 3. There is no outbound network

No zone policy has a network rule, so the OpenShell policy proxy refuses every outbound
connection from a sandbox.

Evidence:
* The conformance check connects to 192.0.2.1:443 (RFC 5737, never routable, no bytes
  sent). The connect fails with `connect: Permission denied`, and the OpenShell
  supervisor logs `NET:OPEN [MED] DENIED /usr/bin/bash(0) -> 192.0.2.1:443
  [reason:transparent_tcp_policy_denied]`. That line is stored verbatim in a PolicyDenied
  event with `source = "openshell_log"`. A missing line fails the check.

## Other controls, briefly

* Agents get no ledger access and no credentials: the host runner is the only ledger
  writer and validates every draft (dedup prefix, agent id and version).
* Inputs are verified: the worker checks every staged file against the host's inputs
  manifest before reading data.
* Every sandbox is tracked in `var/sandboxes.jsonl` and deleted; `meridian sandbox reap`
  cleans up any that were left.

## Known gap

* **Filesystem refusals have no OpenShell log record on 0.1.7.** Landlock returns EACCES
  without any line in `openshell logs`, so file refusals are recorded from what the
  conformance harness observed from outside the sandbox (exit code and stderr,
  `source = "harness"`), not from an independent record. Backlog: kernel-level Landlock
  audit records as an independent source.

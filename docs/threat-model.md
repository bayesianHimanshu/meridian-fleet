# Threat model: the blinding control (phase 2; Agent Substrate zones since phase 4a)

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

## 3. There is no outbound network, except the narrator's one endpoint

No zone policy has a network rule, so the OpenShell policy proxy refuses every outbound
connection from a sandbox. The one exception is rbqm.narrator (phase 2 step 3): its policy
allows `POST /v1/messages` on api.anthropic.com:443 for the python binary only, proved
against its own boundary, and its sandbox gets the key from the meridian-anthropic
provider: the sandbox holds a placeholder and the proxy inserts the key at that endpoint.

Evidence:
* The conformance check connects to 192.0.2.1:443 (RFC 5737, never routable, no bytes
  sent). The connect fails with `connect: Permission denied`, and the OpenShell
  supervisor logs `NET:OPEN [MED] DENIED /usr/bin/bash(0) -> 192.0.2.1:443
  [reason:transparent_tcp_policy_denied]`. That line is stored verbatim in a PolicyDenied
  event with `source = "openshell_log"`. A missing line fails the check.
* `meridian sandbox conformance --policy rbqm.narrator`: GET /v1/models returns 200 through
  the provider, example.com and 192.0.2.1 are refused (both logged), and the
  `ANTHROPIC_API_KEY` inside the sandbox does not start with `sk-ant-` (recorded as true
  or false only). `meridian agents narrate` refuses to run until this passed.

## 4. Zones in Agent Substrate (phase 4a): what changes

Since phase 4a, rbqm, safety.sae_reconciler, quality and quality.audit_trail run in Agent
Substrate actors (gVisor, v1.0.0), one actor per run cloned from a golden snapshot
(docs/substrate-design.md, Part 2). The narrator and the local model, the only zones with
network rules, stay on OpenShell. Facts 1 and 3 hold there too; fact 2 does not.

**Accepted loss: no per-path read allow-list.** Substrate has no Landlock or per-path rule, so
inside an actor every file of the image is readable (`read_etc` in `meridian substrate
conformance` reads /etc/os-release, and is recorded as allowed on purpose). A blinded zone's
actor would read /stage/unblinded if the host put it there; nothing in the sandbox refuses it.
The image holds no study data, so this exposes the image, not the trial. The user accepted
this trade for these four zones in phase 4a.

**The compensating control: an actor receives only its zone's folders.**
* The host stages with the same code as for OpenShell (`stage_run`, then `assert_clean_stage`
  right before the archive is built), so fact 1's evidence covers it.
* The actor refuses any archive member that is not a regular file named `<area>/<file>` of an
  area its template names (`meridian-worker serve --areas`, the policy's reads), so a staging
  bug that adds an area fails the run instead of delivering it.
* Evidence: `test_an_actor_receives_only_its_zones_folders` in
  `tests/unit/test_substrate_executor.py`, for every substrate zone: the archive holds exactly
  the policy's areas, never unblinded/ or the cut manifest; the template's --areas is the same
  set; and the actor's unpack refuses every other area. `test_unpack_refuses_anything_but_
  area_slash_file` and `test_a_link_member_is_refused` cover traversal and links.

**No outbound network (fact 3) holds:** these actors have no EgressPolicy, so atenet-egress
refuses every connection. `meridian substrate conformance` shows it per zone in real actors:
TCP to 192.0.2.1:443, to the host 172.17.0.1:11434, to another actor's worker IP and to the
cluster API is refused, each with a gateway log line (`code=403`, PolicyDenied
source `substrate_log`), and a name outside any policy does not resolve.

**No data at rest** is new here, because Substrate can snapshot a running actor. Controls:
the only snapshot an actor may have is its template's golden snapshot, taken before any data
(the wakeup probe answers only after a constant warm-up); a run is never suspended; the
executor reads the actor back before deleting it and fails the run on any other snapshot;
every actor is deleted. Evidence: `meridian substrate canary` (docs/substrate-eval.md) plants
a unique string in the staged inputs of 20 runs, then searches the snapshot bucket (decoded),
the kind node's filesystem and var/: not found; its positive control suspends one canary actor
and finds the string in that snapshot, so the search would see a leak.

**What is procedure, not enforcement:** the runner acts as Substrate's shared bootstrap
identity, which may suspend actors; nothing in Substrate forbids snapshotting a data actor.
An AccessPolicy without suspend rights for the runner is backlog. Snapshots are not encrypted
and the kind install's rustfs uses its default credentials, acceptable only because no data
snapshot should exist.

## Other controls, briefly

* Agents get no ledger access and no credentials: the host runner is the only ledger
  writer and validates every draft (dedup prefix, agent id and version).
* Inputs are verified: the worker checks every staged file against the host's inputs
  manifest before reading data.
* Every sandbox is tracked in `var/sandboxes.jsonl` and deleted; `meridian sandbox reap`
  cleans up any that were left. Substrate actors likewise in `var/actors.jsonl`
  (`meridian substrate reap`).

* The narrator sees only a finding's kind, site, severity, title, summary and evidence,
  after a de-identification check; the host validates every reply before recording it.

## Known gaps

* **Zone policies differ only in what they may read.** Since phase 3 step 3b each policy
  lists only the staged areas its zone may use, so a blinded zone cannot open
  /stage/unblinded even if a staging bug put a file there (docs/openshell-notes.md, "The
  zones differ in what they may read"). The read-only paths (interpreter, libraries,
  venv) are still the same for every zone.
* **The local model is reached by address, not by identity.** safety.local_model may
  reach 172.17.0.1:11434 (GET /api/tags and POST /api/chat only); whatever listens there
  answers. Ollama's binding (OLLAMA_HOST on the docker0 address) and ufw are the user's
  configuration, not something this code checks. Digest pinning makes a different model
  under the same name fail the run, not pass it.

* **Substrate zones have no read allow-list** (section 4): a staging bug would deliver what it
  staged; the actor refuses only areas its template does not name.
* **Substrate's refusal evidence names the actor, not the policy.** The gateway's line has the
  destination, the actor's SPIFFE id and code 403, but no policy version; the template spec
  hash on the run records what the actor was.
* **Filesystem refusals have no OpenShell log record on 0.1.7.** Landlock returns EACCES
  without any line in `openshell logs`, so file refusals are recorded from what the
  conformance harness observed from outside the sandbox (exit code and stderr,
  `source = "harness"`), not from an independent record. Backlog: kernel-level Landlock
  audit records as an independent source.
* **The narrator's sandbox reads more of the image.** Any network rule makes 0.1.7 add
  `/usr`, `/lib`, `/etc`, `/var/log` and `/proc` to the read-only paths. It holds no study
  data (only the job), so this exposes the image, not the trial.

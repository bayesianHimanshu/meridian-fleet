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
refuses every connection (a refused name lookup leaves no record; see Known gaps). `meridian substrate conformance` shows it per zone in real actors:
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

**Suspend: prevented in our client, not by Substrate (phase 4a step 3).**
* The runner acts as its own identity, the ServiceAccount meridian/meridian-runner with a
  short-lived token, whose only grant is `editor` on the meridian atespace
  (deploy/substrate/access-policy.yaml). Substrate refuses it everything outside the
  atespace: listing atespaces and workers, another atespace, changing the access policy.
* Substrate v1.0.0 cannot refuse an editor suspend, revert or tag: creating and deleting
  actors needs `editor`, and `can_suspend`, `can_revert` and `can_create_tag` are `editor`
  too (cmd/ateapi/internal/authz/model.fga in the release; roles are only owner, editor
  and viewer, cmd/ateapi/internal/apivalidation/access_policy.go; no per-actor grants,
  docs/guides/authorization.md "Known limitations"). `meridian substrate guard-check
  --show-gap` shows it: the runner's identity without the guard suspends a data-free actor.
* So the runner's client carries a verb allow-list (create, resume, proxy, get and delete
  actor; get template and tag) and refuses suspend, revert and tag before anything is sent.
  This is code in the runner's process, not a server control: a compromised host process
  with the token could still suspend. The per-run snapshot check and the canary proof stay
  as the second layer.
* Evidence: `meridian substrate guard-check`, ledger seq 4607 to 4613: three PolicyDenied
  with source `runner_guard` (suspend, revert, tag) and four with source `substrate_authz`
  (the server's PermissionDenied text in raw_line); the actor still RUNNING with only its
  golden snapshot afterwards.

Snapshots are not encrypted and the kind install's rustfs uses its default credentials,
acceptable only because no data snapshot should exist.

**The dispatcher (phase 4b step 2, on kind).** With `[substrate] via = "dispatcher"` the
laptop holds no Substrate token. Each run goes, through one `kubectl port-forward`, to
`meridian-dispatcher` in namespace `meridian-dispatch`. That service runs as its own
ServiceAccount, editor on the atespace only, and it creates, resumes, calls and deletes
the actor in the cluster. What changes:
- The suspend guard now lives in the dispatcher's client (DISPATCHER_VERBS: create,
  resume, get, delete; proxy is refused too). A compromised laptop process can send runs
  but cannot suspend, revert or tag. The remaining gap is the dispatcher's own code, since
  Substrate v1.0.0 still lets an editor suspend.
- The laptop and the dispatcher authenticate each other with a shared token: a bearer
  token with a nonce one way, an HMAC proof of nonce and body the other
  (meridian.sandbox.dispatch). The token sits in a 0600 file outside the repository and in
  a Secret. A request without it is refused with 401 before anything runs.
- atenet-router admits the dispatcher's SPIFFE ID only (phase 4b step 3; until then kind
  also admitted `ate-system/atenet-client`, kubectl-ate's own identity, for the direct
  executor).
- The inputs pass through the dispatcher's memory only. Its root filesystem is read-only,
  its `/tmp` is in memory, and it logs no bodies. The canary searched its pod filesystem
  and logs as well as the bucket and the node, and found nothing; the planted positive
  control was found.
- Actors cannot reach the dispatcher: conformance probe `egress_dispatcher` is refused in
  every zone.

Evidence and numbers: docs/gke-plan.md section 2a.

**The dispatcher alone (phase 4b step 3, kind as GKE).** The laptop's runner identity of
phase 4a (`meridian/meridian-runner`) is deleted, and the dispatcher is the only member of
the atespace's access policy and of atenet-router's ingress allow-list. Even the bootstrap
owner's `kubectl ate proxy` is now reset by the router, so the only path to an actor is
through the dispatcher. Conformance runs through it too (POST /v1/conformance, which
accepts only conformance templates and adds a check that actors cannot reach the GKE
metadata server, 169.254.169.254). Deploy and its golden snapshots still use the
bootstrap owner (ate-setup's client identity); it never runs an agent. On GKE the
dispatcher's Workload Identity principal holds no IAM role, and deploy refuses if any
grant names it. The phase 4a guard-check proved the removed identity; it is retired.

**The leak check (phase 4b step 3).** `meridian leakcheck --env kind|gke` replaces the
canary command. It runs the canary through the dispatcher, then searches:
- the snapshot bucket (decoded);
- Cloud Logging over the run window (GKE);
- the dispatcher pod and its logs;
- every node's filesystem, from a read-only scan pod on each node;
- the laptop's var/.

Every source needs a positive control found, or the check fails. On kind it passed for the
three zones and then with safety included (docs/gke-plan.md section 2b); the safety zone
moves to GKE only after it passes there.

**Inputs held for a pass (phase 4b step 4).** A run's inputs no longer travel whole per
run: each distinct staged file goes once per pass into the dispatcher's in-memory input
store, by sha256, and a run sends references (docs/input-staging.md). So subject-level
inputs now rest in the dispatcher's process memory for the length of a pass, not of a
request. The store is dropped (buffers zeroed) at the end of every pass and after 15
minutes idle, is never written to disk or logged, and accepts only blobs whose sha256 is
their name. The dispatcher rebuilds each archive and checks its sha256, and the actor
still checks the archive and the inputs manifest, so a wrong or swapped blob fails the run.
Freed copies in the process heap last until the process exits (park and teardown stop it).
The leak check has a source for the store: a planted file held during the pass, nothing
after the drop.

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
* **Suspend is refused by our code, not by Substrate** (section 4). Substrate 1.0.0 grants
  `can_suspend`, `can_revert` and `can_create_tag` to `editor`, the least role that can
  create and delete actors, so the runner's identity may suspend as far as the server is
  concerned (`meridian substrate guard-check --show-gap` shows it). The guard that refuses
  it is the verb allow-list in the runner's own process (`RUNNER_VERBS` in
  src/meridian/sandbox/substrate.py). Anything running as the host user with the runner's
  token (var/substrate/runner.token, 0600, short-lived) could suspend a live actor and leave
  its inputs in a snapshot. What would catch it: the per-run check that the actor holds only
  its golden snapshot before deletion (fails the run), and the canary proof. Closing it
  needs a Substrate role without suspend, or a per-actor grant, neither of which 1.0.0 has.
* **Substrate refuses a name lookup without any record.** A name outside every policy does not
  resolve inside an actor (the probe sees NXDOMAIN), but neither atenet-egress nor any other
  Substrate component logs the refused lookup (v1.0.0 docs say DNS refusals are not logged).
  So a lookup refusal is recorded from what the probe saw (`source = "harness"`), like
  OpenShell's file refusals below, not from an independent record. TCP refusals do have the
  gateway's line. Per Substrate's docs (docs/guides/network-egress.md in the release), the
  sandbox's DNS relay answers NXDOMAIN itself for an actor without an egress policy and
  forwards nothing, so the name asked for does not leave the worker; that is the vendor's
  statement, not something this project observed.
* **Each run's actor is reached through its own host process, and that is the throughput
  limit.** A run starts `kubectl ate proxy actor`, which mints a client certificate and
  forwards an ephemeral loopback port (127.0.0.1) to the actor for the length of the run.
  At 16 concurrent runs its start grew from 0.57 s (one at a time) to 2.6 s p50, and
  throughput stopped rising past about 12 (docs/substrate-eval.md, worker size); the host's
  CPU, one kubectl and port-forward per run, is the limit, not the cluster. Two
  consequences for this model: availability, not confidentiality, is what degrades under
  load (no run failed in 200 at 16, but a slower start eats into the 120 s placement wait);
  and while a run lasts, any process of the host user could connect to that loopback port
  and reach the actor's worker endpoint, which takes the staged archive and returns drafts.
  The host is trusted in this model (it stages the data in the first place), so this adds
  no new reader of study data, but it is a reason a long-lived router connection with one
  client certificate, owned by the runner, would be better than a process per run.
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

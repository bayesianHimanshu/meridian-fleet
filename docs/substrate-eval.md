# Agent Substrate: proofs and measurements (phase 4a steps 2 and 3)

Step 2's proofs and measurements come first; step 3's (the runner's identity, the slow
restores, worker size, the full pass) are in "Step 3" at the end.

Setup: Agent Substrate v1.0.0 (`b34fbee7`) on a single-node kind cluster (Kubernetes 1.37)
on this laptop (16 CPUs, 15 GiB RAM), gVisor only. One WorkerPool, meridian/meridian, of
gVisor workers (1 CPU and 1 GiB each). Agent image `meridian-agent:0.1.0`, image id
`sha256:21f775d6...e5498`, pushed as `localhost:5001/meridian-agent@sha256:ce7bdedb...0e5587`
and built from commit 41fd1e9. Later commits change only host code (the API's run trace and
these docs); the contract test shows the worker never imports it. Raw results are in
var/substrate/ (canary.json, bench.jsonl, verify12.md, conformance.txt), which is not
committed. The commands are under `meridian substrate`.

## The proofs

### C1. No data at rest: passed

`meridian substrate canary --runs 20`: a unique string `MERIDIAN-CANARY-<32 hex>` (new on each run; sha256 of the reported
one `918dc543...`) replaced AETERM in one row of
blinded/ae.parquet of a copy of DC-BASE, and DECISION_REASON in one row of a copy of the audit
extract. Both files were written uncompressed, and the canary was checked to be in their bytes.
Then 20 runs went through SubstrateExecutor, cycling rbqm.kri_monitor, rbqm.qtl_detector,
rbqm.csm, safety.sae_reconciler, quality.tmf_completeness and quality.audit_trail_reviewer.
Every zone received the canary: the first four zones read blinded/ae.parquet, and the audit zone
reads the extract.

| Store searched after the 20 runs | Items | Bytes searched | Canary found |
|---|---:|---:|---|
| Snapshot bucket (rustfs `ate-snapshots`, every object over S3, sparse-zstd decoded) | 40 (35 decoded) | 0.66 GB | no |
| kind node filesystem: worker pods, `/var/lib/ate` (snapshot cache and scratch, actors, workers), pod logs, containerd, the rustfs and Postgres volumes (tar stream; zstd and gzip decoded) | 68,830 files (14,515 decoded) | 10.7 GB | no |
| Host `var/` (actors.jsonl, sandboxes.jsonl, logs) | 28 | 7.6 MB | no |

No suspend happened:
- The bucket held only `atespaces/meridian/tags/<uuid>/...`, five tags, each the golden
  snapshot of a current template (four zones and conformance). No object was under `actors/`.
- Each run read its actor back before deleting it and found only its golden snapshot (owner
  TAG); any other snapshot fails the run.
- No actor was left afterwards.

**Positive control** (same command): one more actor of the rbqm template got a canary run and
was then suspended (`kubectl ate suspend --mode sync`). The search found the canary in that
actor's `actors/<uid>/snapshots/<uuid>/pages.img.zstd`, the memory image, after the server had
deleted its files and returned. So the search finds a leak when there is one, and a run that
is suspended would leak. After the actor was deleted, its 8 snapshot objects were gone within
30 s and the search came back clean.

The first attempt of this proof found nothing in the positive control either: snapshots use
Substrate's own sparse format (`ATESPRSE`, a version, then a zstd stream), which a plain zstd
decoder rejects. The decoder was fixed and validated before the reported run, so the search
now sees inside them.

### C2. Same results: passed, 12 of 12 byte-identical

`meridian substrate verify`: the 12 agent and cut pairs of phase 2 step 1 (rbqm.csm,
rbqm.kri_monitor and rbqm.qtl_detector on DC-BASE, DC-RBQM-CORE, DC-NULL-S6 and DC-d5ada06c)
through OpenShell and through Substrate. The drafts documents match byte for byte in every
pair. The same holds for the 20 canary runs and for the cluster test
`tests/substrate/test_cluster.py` (Substrate against in-process).

### C3. No network: passed, all four zones

`meridian sandbox conformance --executor substrate` (also `meridian substrate conformance`).
For each zone it creates two actors from the conformance template; the first is the probe.

| Check | Outcome in every zone |
|---|---|
| TCP to 192.0.2.1:443 | refused |
| TCP to the host 172.17.0.1:11434 (Ollama) | refused |
| TCP to the other actor's worker IP, port 8080, where it listens | refused |
| TCP to 10.96.0.1:443 (the cluster API) | refused |
| resolve api.anthropic.com | refused (no such name) |
| write /opt/meridian/conformance | refused (EACCES) |
| control: write /tmp | allowed |
| control: TCP to its own 127.0.0.1:8080 | allowed, the probe's own HTTP reply |
| control: the other actor answers /readyz through the router | allowed |
| read /etc/os-release | allowed, the accepted loss (no read allow-list) |

What a refusal looks like here differs from OpenShell. `connect()` succeeds inside the actor,
because atunnel accepts every TCP connection. The tunnel then resets it before any byte comes
back (errno 104). The evidence is atenet-egress's access log, one line per refused connection,
naming the destination and the actor's SPIFFE id:

    [egress] authority=192.0.2.1:443 peer_san=spiffe://substrate-actor.local/worker-for-actor/meridian/mer-cf-ef3e04ef ... protocol=HTTP/2 code=403 flags=- up_bytes=0 down_bytes=13

A zone passes only if every network target has such a line. All four zones had all four lines.
They are recorded as PolicyDenied with source `substrate_log` (6 per zone: the 4 TCP checks with
their line, plus the name and the write as `harness`), and a ContainmentCheckCompleted with
executor substrate, at ledger seq 4559 to 4586. Name refusals are not logged by Substrate (its
docs say so); only the probe saw NXDOMAIN.

### C4. Zone folders: passed

`tests/unit/test_substrate_executor.py::test_an_actor_receives_only_its_zones_folders`, run in
CI, covers every substrate zone:

| Zone (policy) | Areas in the archive |
|---|---|
| rbqm | meta, blinded, ops |
| safety.sae_reconciler | meta, blinded (with the safety database's blinded area) |
| quality | meta, blinded, tmf |
| quality.audit_trail | meta, audit |

The test checks that no archive member is unblinded/, ground truth or the cut manifest, that
the template's `--areas` is the same set, and that the actor's unpack refuses every other area.
Further tests refuse traversal, nested paths, hidden files and links.

### C5. DC-BASE through Substrate, accepted by the audit reviewer: passed

`meridian agents run --cut DC-BASE --rerun` with the default zoned executor ran all 5 DC-BASE
agents in Substrate (seq 4587 to 4596). No finding was appended, and the one draft was skipped
as already raised, so the results are the same as before. `meridian audit review` then ran
quality.audit_trail_reviewer 1.1.0, itself in Substrate, over seq 1 to 4596. It raised 7
findings (seq 4599 to 4605), the same 7 phase 1 runs without a sandbox that 1.0.0 raised, and
none about the Substrate runs. `meridian ledger verify`: ok, 4,606 events.

## Measurements

Every run is rbqm.kri_monitor on DC-BASE. OpenShell is run with synchronous deletion, so a
run's time includes its teardown, as in the baseline.

| | OpenShell (phase 2 baseline) | OpenShell, measured now (100 runs) | Substrate (100 runs) |
|---|---:|---:|---:|
| Run, p50 | about 8 s (5.4 s of it deletion) | 10.09 s | **1.20 s** |
| Run, p95 | | 10.20 s | **1.30 s** |
| Run, max | | 10.23 s | 3.74 s |
| First run after deploy | | | 1.5 to 1.6 s |
| Failures | | 0 | 0 |

Substrate steps (100 warm runs; p50 / p95 / max, in ms):

| Step | p50 | p95 | max |
|---|---:|---:|---:|
| stage and tar (host) | 9 | 12 | 15 |
| actor create | 83 | 96 | 110 |
| restore from the golden snapshot (resume) | 225 | 284 | 2,815 |
| `kubectl ate proxy` start (client certificate, port forward) | 570 | 582 | 590 |
| request: unpack, verify, run the agent, reply | 94 | 123 | 160 |
| actor delete | 216 | 240 | 330 |

- **Golden snapshots** were ready 10 to 28 s after a template was created (5 templates at
  once, 2 workers).
- **The first run after deploy** took 1.5 to 1.6 s. Its resume, 520 to 590 ms, is about twice
  a warm one.
- **Slow resumes:** 1 run in 100 sequential, and 1 in 20 at 2 workers, resumed in about
  2.8 s. Cause not established (**guess**: a worker restoring a template's snapshot for the
  first time).
- **The proxy start is now the largest step** (0.57 s of 1.2 s). A long-lived router
  connection, made once per pass, would remove most of it.

**Memory per worker** (each worker pod's cgroup on the node):
- Idle: 20 to 55 MB.
- Peak while running these agents: about 250 MB.
- The two workers that also built the golden snapshots peaked at 330 and 370 MB.
- The pod is limited to 1 GiB and the actor to 768 MiB; both have room to spare.

**Concurrency** (pool scaled to K workers, K runs at a time, 10 rounds):

| K | Runs | Failed | p50 | p95 | Host memory free after |
|---:|---:|---:|---:|---:|---:|
| 2 | 20 | 0 | 1.20 s | 3.81 s | 4.99 GB |
| 4 | 40 | 0 | 1.27 s | 1.44 s | 4.62 GB |
| 6 | 60 | 0 | 1.43 s | 1.57 s | 4.39 GB |
| 8 | 80 | 0 | 1.60 s | 1.73 s | 4.48 GB |
| 10 | 100 | 0 | 1.97 s | 2.59 s | 4.15 GB |
| **12** | **120** | **0** | **2.50 s** | **2.95 s** | **3.91 GB** |
| 14 | | | | | pool cannot be placed |

**The largest number of concurrent runs this laptop sustains is 12.**
- At 14 workers, two pods stay Pending ("Insufficient memory"). The node runs out of memory to
  *reserve* (each worker requests 1 GiB), not memory in use: 3.9 GB was still free at 12.
- With 512 MiB workers, about twice as many would fit, given the 250 MB peak. That is not tested.
- Above 8 the proxy start grows (p95 1.6 to 2.1 s), so the host side is the bottleneck.
- More concurrent runs than workers do not queue: the extra actors fail at once with
  `ResourceExhausted: no worker has room for the actor` (14 of 20 at 4 runs on 2 workers).
  The runner runs one at a time, so this does not happen today; a parallel runner must cap at
  the pool size.

**Against the 8 s baseline:** a Substrate run is 6.7 times faster at p50 than the phase 2
baseline, and 8.4 times faster than OpenShell measured now on the same agent, cut and laptop.
Today the OpenShell run is 10.1 s rather than 8 s, probably because the cluster shares the
laptop. A pass of the 6 DC-BASE agents takes about 7 s instead of about a minute.

## Step 3

### Slow restores: the node's image cache evicts the agent image

`meridian substrate bench` now writes one row per run (actor, worker pod, start time,
steps) to its JSON line (`runs_each`). 200 sequential runs (var/substrate/restore.jsonl):
p50 1.19 s, p95 1.26 s, one slow run, the first (resume 3,009 ms). atelet's "Restore
timing breakdown" line for that actor: `oci_unpack` 2.77 s (median of 220 restores:
0.5 ms), snapshot download 0.3 ms, gVisor restore 0.15 s. atelet had logged `Image cache
miss` for the agent image just before, and `Image cache GC pass complete ... evicted_images
1, evicted_layers 9` (574 MB) at the previous 5-minute GC pass.

atelet's image GC (`cmd/atelet/imagegc.go` in v1.0.0) runs every 5 minutes and, while the
cache volume is above 85% full, evicts images older than 2 minutes that no actor uses. The
kind node's /var is this laptop's disk, 89% full. Five benches of 4 runs after idle gaps
(var/substrate/restore-gaps.jsonl) match the GC schedule exactly:

| Idle gap before the bench | GC pass with the image idle over 2 min? | First resume |
|---|---|---:|
| 0 s (twice) | no | 242 ms, 252 ms |
| 60 s | no | 209 ms |
| 180 s | no (the 20:50:04 pass saw 1 min 43 s) | 192 ms |
| 300 s | yes (20:55:04, evicted) | **2,817 ms** |

The second run of the 300 s bench, on the other worker, resumed in 220 ms: the cache is
per node, not per worker. So this is not a first restore per template or per worker, and
pre-warming workers after deploy would not help (the image is evicted again after a few
idle minutes). Fixes are outside this project's remit: free space on the laptop's disk, or
raise atelet's `--image-cache-high-percent` in ate-system. A pass pays it once, at its
first wave of runs (they share one pull).

### Worker size: 512 MiB workers, 384 MiB actors (kept)

A 512 MiB worker offers 448 MiB to actors, so the actor limit went from 768 to 384 MiB
(templates `<zone>-3f954d2c8c3e-m384`). All six agent types ran three times each in 384
MiB actors without failure; worker pods peaked at 266 to 289 MiB. Concurrency was raised
with the pool scaled to K workers, K runs at a time, while a sampler recorded the lowest
host MemAvailable; the ladder stopped before a level whose predicted minimum was below
1.5 GB (each extra concurrent run costs about 210 MB of host memory). Raw: 
var/substrate/size512/.

| Workers | K at a time | Runs | Failed | p50 | p95 | Min host MemAvailable |
|---|---:|---:|---:|---:|---:|---:|
| 1 GiB (step 2) | 12 | 120 | 0 | 2.50 s | 2.95 s | (3.91 GB after) |
| 512 MiB | 12 | 60 | 0 | 2.14 s | 2.68 s | 2.59 GB |
| 512 MiB | 16 | 80 | 0 | 2.94 s | 3.56 s | 1.79 GB |
| **512 MiB** | **16** | **200** | **0** | **3.52 s** | **5.57 s** | (4.23 GB after) |
| 512 MiB | 20 | | | | | stopped: predicted 0.95 GB |

- **The largest sustained concurrency is 16** (the limit is now the laptop's memory, not
  the pods' reservations: 14 x 1 GiB could not be placed). 200 runs at 16 had no failure,
  so the size is kept: `[substrate] worker_memory_mib = 512`, `actor_memory_mib = 384`.
- **More workers do not mean more throughput here.** 16 at a time finished about 4.5 runs
  per second, 12 at a time about 4.8 to 5.6. In the 200-run test the `kubectl ate proxy`
  start grew to 2.6 s p50 (0.57 s sequentially): the host's CPU, one kubectl process and
  port-forward per run, is the bottleneck. 12 workers is the efficient setting on this
  laptop; the config stays at 2 workers when idle.

### The full pass on the final image

Image `sha256:ee5a9a2b...` built from 1dfee1b (clean), templates `<zone>-49d1c5259345-m384`;
conformance passed in all four zones (seq 6529 to 6556). The Substrate part of the pass,
`meridian agents run --rerun --all-cuts` with the pool at 12 workers, then the audit review
with the pool back at 2, each run alone with a sampler stopping it below 1.5 GB of host
MemAvailable. Raw: var/substrate/fullpass/.

| Step | Runs | Failed | Wall-clock | p50 | p95 | Max | Min host MemAvailable | Seq |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Four zones, every cut, 12 at a time | 910 | 0 | 198.4 s | 2.56 s | 3.07 s | 3.41 s | 1.96 GB | 6629 to 8448 |
| Audit review 1.2.0, seq 1 to 8448 | 1 | 0 | 4.1 s | | | | 4.06 GB | 8449 to 8458 |

- Every ActorWoke names commit 1dfee1b and the new image and carries `started_at`.
- 12 at a time gives about the same wall-clock as 16 did on the previous image (196.9 s for
  the same 910 runs), with lower per-run times (p50 2.56 s against 3.52 s in the 200-run
  test at 16): the host side, not the workers, sets the pace.
- The reviewer raised 7 findings (QA-0164 to QA-0170, high): runs of rbqm.csm,
  rbqm.kri_monitor and rbqm.qtl_detector on DC-BASE in phase 1 without a sandbox record,
  the same 7 that 1.1.0 raised in C5. Nothing about any Substrate or OpenShell run of
  phase 4a.
- The local narrator could not finish: with phi4-mini loaded, llama-server peaks at 3.65 GB
  and host memory fell below 1.5 GB in 90 to 110 s, even with the pool at 0
  (docs/phase4a-summary.md). 104 of 173 local narrations were recorded; 69 stay pending.

## Phase 4b step 2: through the in-cluster dispatcher

The proofs and measurements of the dispatcher on kind are in docs/gke-plan.md section 2a.
In brief:

- **C1 canary** (20 runs): found nowhere, including the dispatcher's pod filesystem and
  logs.
- **C2:** 12 of 12 pairs byte-identical.
- **C3:** passes in all 4 zones, plus an actor-to-dispatcher probe, refused.
- **Unauthenticated requests:** 401.
- **Audit reviewer:** accepts the dispatched runs.
- **Full pass:** 81.3 s at 12 workers (p50 1.12 s, p95 1.33 s), against 198.4 s direct;
  84.3 s at 16.
- **Throughput limit:** levels off at 10 to 11 runs/s, set by Substrate's own path on the
  single kind node (not the tunnel, the dispatcher's CPU or host CPU).

Raw data:
- `var/substrate/canary-dispatcher.json`;
- `var/substrate/bench-dispatcher.jsonl`;
- `var/substrate/fullpass-dispatcher/` (`pass12.json`, `pass16.json`, the runner's output);
- ledger seq 8491 to 12133.

## Phase 4b step 3: the dispatcher alone, the leak check as code

On kind, the dispatcher is now the only Substrate identity Meridian has: the laptop's
runner ServiceAccount is deleted, and the access policy and atenet-router's allow-list
name only the dispatcher. Conformance runs through it, with a new metadata-server check.
`meridian leakcheck` replaces `substrate canary`: every source searched has a positive
control, and the check fails if one is missing. The phase 4a guard-check proved the
removed identity and is retired; its results above stand.

On kind it passed for the three zones and then with safety included. Full passes at 12
workers with the node tainted ran at the step 2 speed (81.5 s). A pass with worker pods
force-deleted found that a worker lost during resume was not retried; fixed, all three
losses were retried on new actors. An identical rerun now records nothing. Details and
numbers: docs/gke-plan.md section 2b; the GKE session: docs/gke-session.md.

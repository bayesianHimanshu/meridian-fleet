# Phase 4b summary: the Substrate zones on GKE

Phase 4b put the four no-network zones (rbqm, safety.sae_reconciler, quality,
quality.audit_trail) on Agent Substrate v1.0.0 in a GKE Standard cluster (us-central1-a),
driven from the laptop through one authenticated tunnel. Phase 4a closed at seq 8,458;
phase 4b is seq 8,459 to 19,600, and `meridian ledger verify` is ok at 19,600 events. The
plan and every session's detail: docs/gke-plan.md; the input design: docs/input-staging.md.

| Step | PR | What it added |
| --- | --- | --- |
| 1 | 25 | Decisions survive agent upgrades; the GKE plan, nothing created |
| 2 | 26 | The in-cluster dispatcher, on kind; account check, park and resume in the plan |
| 3 | 27 | GKE deploy code, the dispatcher as the only identity, the leak check as code |
| 4 | 28 | The first GKE session; the taint and access policy fixes |
| 4 | 29 | Inputs by content, park without waiting, env.example.sh; the second GKE session |

## What moved and what stayed

- **On GKE:** Substrate (ate-system, Postgres on a 10 GiB claim), the WorkerPool on a Spot
  pool of 0 to 2 `c3-standard-4` nodes tainted `ate.dev/sandboxClass=gvisor:NoSchedule`,
  golden snapshots in a GCS bucket, the images in Artifact Registry, and the dispatcher.
- **On the laptop:** the runner, the ledger and its only writer, staging of each run's
  inputs, every check of an answer, the narrator and the local model (OpenShell), and the
  kind cluster, which still runs everything for development and CI-adjacent tests.
- **Google Cloud changes were yours** (part 1 of docs/gke-session.md, teardown.sh); mine
  were Kubernetes objects and read-only gcloud.

## The dispatcher and the input store

- `meridian-dispatcher serve` runs in the cluster and is the only Substrate identity
  (editor on the `meridian` atespace, no Google Cloud role). The laptop reaches it through
  one `kubectl port-forward` over the cluster's DNS endpoint, with a bearer token and an
  HMAC proof on every answer. It refuses suspend, revert, tag and proxy before Substrate is
  asked; atenet-router admits only it.
- **Inputs by content (step 4):** each distinct input file goes once per pass into a
  MemoryInputStore in the dispatcher, keyed by sha256; a run sends a RunRef (member
  digests plus its trigger and manifest). The dispatcher rebuilds the archive byte for
  byte and checks its sha256; the actor checks the archive and inputs manifest as before.
  The store is dropped (buffers zeroed) at the end of every pass, or after 15 idle
  minutes; nothing touches disk; the process starts with core dumps off (`ulimit -c 0`).

## The proofs and their evidence

| Proof | Result | Evidence |
|---|---|---|
| Leak check on GKE: 2 in session 1, 4 in session 2 | canary in none of the sources below; every positive control found | var/leakcheck/gke-20261114T*.json |
| Conformance, four zones | every egress refused (dispatcher, test net, host, peer actor, API server, metadata server, DNS); controls allowed | seq 15853 to 15888 (session 1), 17709 to 17780 (session 2) |
| Full pass on GKE | 910 runs, 0 failed, drafts and inputs manifests checked per run | seq 17781 to 19600 |
| Rerun appends nothing | 910 runs again, 0 events appended (19,600 before and after) | ledger verify |
| Same drafts as kind | 910 of 910 drafts and manifests equal a recorded run of the same agent and trigger | kind pass seq 15889 to 17708 |
| Placement | workers only on labelled, tainted nodes with a Ready atelet; the dispatcher off them | `substrate placement-check`, 8 checks |
| No role, no extra identity | the dispatcher's Workload Identity principal in no IAM policy (6 read) | deploy output |

Leak check sources (session 2, 24 runs, all four zones):

| Source | Searched | Positive control |
|---|---|---|
| dispatcher input store | 16 blobs, 3.3 MB | 2 of 2 planted files held during the pass, nothing after the drop |
| dispatcher pod filesystem | 11,267 files, 637 MB | marker in its /tmp |
| dispatcher logs; Cloud Logging | 1 log; 23,121 entries | marker logged as a request path |
| snapshot bucket | 40 objects, 663 MB, each golden snapshot decoded | the worker's path in each snapshot's memory |
| each node, scanned in place | about 160,000 files, 33 to 36 GB per node | marker in the scan pod's own layer |
| the laptop's var/ | about 110 files | marker file |

After each full pass the dispatcher was restarted (the old pod confirmed deleted) and the
leak check rerun on the new pod: pass. Nodes have no swap.

## Measurements, kind and GKE

Per run from ActorWoke `started_at` to ActorSuspended, from the ledger; full pass is 910 runs.

| | kind, archive per run | kind, by content | GKE session 1 | GKE session 2, by content |
|---|---:|---:|---:|---:|
| Per run p50 / p95 | 1.12 / 1.33 s | 1.17 / 1.45 s | about 10 s | 7.02 / 12.39 s |
| Full pass | 81.3 s | 86.2 s | not run (about 1.7 h projected) | **270 s** (24 at a time) |
| Runs per second | 11.2 | 10.6 | 0.11 (48-run bench) | 3.37 (pass); bench 2.7 to 3.9 |
| Bytes sent per pass | 814.2 MB | 15.9 MB | 942 KB a run | 15.9 MB |

GKE benches, 200 runs: one worker node at 12 at a time 2.69 runs/s; two nodes 3.18 (12) and
3.93 (24). A second node trims the in-cluster phases (request p50 467 to 271 ms) but each
run's tunnel phase stays about 1 s p50 and 19 s p95: the port-forward is the ceiling.

## Costs per session (USD, estimates)

| Session | Cluster up (UTC, 14 Nov) | Estimate |
|---|---|---:|
| 1 | 06:17 to 09:54 | about 1.21: fee 0.36, system node 0.69, Spot 0.11, egress 0.05 |
| 2 | 13:29 to 17:06 | about 2.29: fee 0.36, system node 0.74, Spot 0.29, egress about 7.5 GB 0.90 |
| Phase 4b | | **about 3.50 (about INR 310)**; nothing bills since the teardown |

The real figure: there is no billing export to read from the CLI (no BigQuery dataset),
so it is Billing > Reports, grouped by SKU, for 14 to 15 Nov. Budget: INR 2,500 a month.

## Gaps

- **Tunnel ceiling.** One port-forward over the DNS endpoint carries every request; runs
  per second on GKE are a third of kind's. Several port-forwards, or the runner closer to
  the cluster, would be the next measurement.
- **Suspend permission.** Substrate v1.0.0 has no role that runs actors without suspend,
  revert and tag; only the dispatcher's code refuses them, so a compromised dispatcher
  could suspend an actor and put a run's memory in the bucket.
- **Unlogged DNS refusals.** Conformance shows lookups fail inside an actor ("Name or
  service not known") and the metadata server has no route, but nothing logs those
  attempts: atenet-egress records only connects that reach the gateway.
- **Leak check egress.** Each GKE leak check downloads the bucket and the dispatcher pod
  (about 1.3 GB, USD 0.16), most of session 2's egress; scanning them in the cluster would not.
- **Freed heap copies** of inputs stay in the dispatcher's process until it exits; the
  restart after a pass is the control, not a zeroing.

## Lessons

- Kind hid three GKE facts: a taint atelet does not tolerate, a new atespace with no
  access policy (exit 0, no output), and PDBs that stall a drain to zero nodes.
- Measure before designing: inputs repeat by file, not by set (731 files, 12.3 MB, against
  476 sets, 397 MB), which made an in-memory store enough and a bucket unnecessary.
- A positive control in every source paid off: one leak check "found nothing" only
  because the autoscaler cut its stream, and the control failed it.
- Size the WorkerPool for the checks: 24 workers left no CPU for a node scan pod.
- Park by stopping workloads first; without free credit, tear down between sessions.

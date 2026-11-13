# Phase 4b plan: the Substrate zones on GKE (us-central1)

Phase 4b step 1, updated in step 2 with the account check, your answers, the dispatcher as
built and measured on kind (section 2a), and the park, resume and teardown scripts, and in
step 3 with the GKE code exercised on kind (section 2b) and the session runbook
(docs/gke-session.md). This is
still a plan: it creates nothing in Google Cloud, and every command in it is for a person
to run, in order. The rbqm,
safety, quality and quality.audit_trail zones move from Agent Substrate on kind to Agent
Substrate on GKE. The narrator (OpenShell provider to Anthropic) and the local model zone
(Ollama) stay on OpenShell on the laptop, and the ledger stays on the laptop with one writer.

**Sources.** Substrate at **v1.0.0** (`b34fbee742906494b5eb4f1f36987176bf8770a4`, read-only
checkout at ~/src/substrate). At v1.0.0 `tools/setup-gcp` is **no longer in the Substrate
repo**: it moved to github.com/ai-on-gke/substrate-gke (Substrate's `AGENTS.md:69`,
`docs/guides/operator-install.md:355`; the last Substrate tag with its own copy is
v1.0.0-rc.1). This plan reads substrate-gke at **v1.0.0-gke.0**
(`c3deab4ab5a08622c311ca29d429fc1fe6731895`, 6 Nov 2026, read-only at ~/src/substrate-gke),
its only tag. Paths are relative to those checkouts. Google's documentation and pricing
pages are cited where used, read on 13 Nov 2026. **Guess** marks anything neither the code,
the docs nor gcloud states.

## 1. Account check

Read-only commands, 13 Nov 2026, gcloud 539.0.0, configuration `bayesianhimanshu-org`
(the GKE and Compute APIs were enabled by you during step 2):

| Check | Result |
|---|---|
| `gcloud projects describe meridian-fleet-agents` | ACTIVE, number 932415716644, created 2026-11-07 |
| `gcloud billing projects describe` | billing enabled, account `billingAccounts/01DC09-5AAF08-94EFA4` ("bayesianHimanshuBillingAccount", open); a second account, "My Billing Account", is closed |
| Projects on that billing account | `meridian-fleet-agents` and one other project. I did not read the other one (outside this brief), so whether a GKE cluster there uses the free tier credit is **your answer** (open question 1) |
| Enabled APIs before the plan | `container`, `compute`, `logging`, `monitoring`, `cloudtrace`, `telemetry`, `storage` (and storage-api, storage-component), `serviceusage`, `servicemanagement`, the BigQuery family, `datastore`, `dataplex`, `dataform`, `analyticshub`, `apptopology`, `cloudapis`, `sql-component`. Not yet: `artifactregistry`, `iam`, `iamcredentials`, `cloudresourcemanager`, `networkconnectivity`, `billingbudgets` (6.1 and 6.2) |
| `gcloud container get-server-config --location=us-central1` | Regular: default **1.36.4-gke.2377000**, valid **1.37.1-gke.1552000**. Rapid: default 1.37.1-gke.2097000, also 1.37.2-gke.1042000. Stable: no 1.37. Extended: 1.37.1-gke.1552000 (not usable). Valid node versions include 1.37.1-gke.1552000 |
| Google's release schedule | 1.37 in Regular since 2026-10-22, in Stable estimated 2027-01 (https://docs.cloud.google.com/kubernetes-engine/docs/release-schedule) |
| `gcloud compute machine-types describe c3-standard-4` | 4 vCPU, 16,384 MB, offered in us-central1-a and us-central1-c |
| GKE clusters in the project | none |

**Consequence:** Regular's default is 1.36, below Substrate's minimum, so the cluster is
created with `--cluster-version=1.37.1-gke.1552000` on the Regular channel (6.4).

**Machine type, the exact sentence.** Google's install guide, "Cluster requirements"
(https://docs.cloud.google.com/kubernetes-engine/ai-ml/install-overview-substrate, read
13 Nov 2026): "Uses `c3-standard-4` or larger machine types for nodes." Its "Limitations"
add: "you can't use machine series that run on mixed CPU architectures (such as E2 machine
types)." The guide names `c3-standard-4`, so by your rule **C3 stays** for both pools and
the cost table in section 5 is unchanged.

| Requirement | Source | This plan |
|---|---|---|
| GKE **1.37 or later**, control plane and every node pool | install guide: "Runs GKE version 1.37 or later on both the control plane and the node pools."; substrate-gke README lines 125-130 | 1.37.1-gke.1552000, Regular |
| **Standard**, not Autopilot | install guide | Standard |
| Not the **Extended** channel | README lines 140-142; install guide | Regular |
| **Workload Identity Federation** | README lines 400-402; install guide | `--workload-pool=$PROJECT_ID.svc.id.goog` |
| **Dataplane V2** | install guide: "Uses GKE Dataplane V2." | `--enable-dataplane-v2` |
| **No E2**, no Arm | install guide, design page | no E2 node anywhere |
| Nodes `c3-standard-4` or larger | install guide (quoted above) | `c3-standard-4` |
| APIs | setup-gcp `cmd/api.go:35-46` | 6.2 |

## 2. Topology: how the laptop reaches Substrate on GKE

### What has to happen per run

Today the runner calls `kubectl ate` for each verb (create, resume, get, delete) and opens
one `kubectl ate proxy actor` per run. Each `kubectl ate` call without `--endpoint`
"automatically port-forwards" to ate-api-server (`docs/reference/kubectl-ate.md:33`), and
`proxy actor` mints a client certificate and opens an mTLS `CONNECT` tunnel to
atenet-router (`docs/reference/api-guide.md:250`). On kind every one of those is a local
process; phase 4a measured that the per-run proxy process is what stops throughput rising
past about 12 runs at once (docs/phase4a-summary.md, Open gaps). On GKE each of them would
also be a fresh round trip from the laptop through the control plane.

The cluster setup-gcp creates has **no IP endpoint for the control plane**: it is reachable
only through its DNS endpoint, where IAM (`container.clusters.connect`) authorizes each
request before the API server sees it (substrate-gke `tools/setup-gcp/README.md:76-81`;
`cmd/cluster.go:150-160` sets `IpEndpointsConfig.Enabled=false`). atenet-router has no
external address either and admits a client only if its SPIFFE ID is in
`ingressAllowedSPIFFEIDs`, which has no default (`docs/guides/operator-install.md:394-405`).

### Options

| | How | Per-run cost | Exposure | Verdict |
|---|---|---|---|---|
| A. Dispatcher in the cluster, one tunnel | A small `meridian-dispatcher` Deployment in namespace `meridian` does create, resume, POST /run through atenet-router, snapshot check and delete, in the cluster. The laptop holds **one** long-lived `kubectl port-forward` to it, over the DNS endpoint. | One HTTPS request over an open tunnel | Nothing new: no Service of type LoadBalancer, no Ingress, no external IP | **Recommended** |
| B. Today's executor, unchanged | `kubectl ate` per verb, `kubectl ate proxy` per run, over the DNS endpoint | About 5 port-forwards and one proxy per run, each a TLS handshake through Google's front end | Same as A | Works with no code, keeps the throughput limit and adds WAN latency to each verb |
| C. Internal load balancer plus IAP | atenet-router or a dispatcher behind an internal passthrough load balancer; the laptop reaches it with `gcloud compute start-iap-tunnel` through a small bastion VM | One request over the IAP tunnel | A bastion VM and an internal forwarding rule to pay for and secure | More parts than A for the same result (IAP TCP forwarding targets VM instances, not Services) |
| D. Cloud VPN to the VPC | HA VPN from the laptop | One request | Needs a VPN gateway on the laptop's side; per-tunnel hourly charge | Too heavy for one laptop |
| E. Public endpoint with mTLS | A LoadBalancer Service with client certificates | One request | A public address | Ruled out by the brief |
| F. Queue in Google (Pub/Sub or a bucket) | The laptop drops inputs, the cluster picks them up | Asynchronous | Subject-level inputs **at rest** in Google | Ruled out: inputs must not rest |
| H. Connect gateway (fleet) | kubectl through connectgateway.googleapis.com | One request over an open session | Same as A | No price listed, but limited to 10 active streams per fleet host project and sessions that end after about 20 to 30 minutes (Google's Connect gateway page): too tight for 12 to 32 concurrent runs |
| G. Third-party mesh (Tailscale operator and the like) | A device-to-pod overlay | One request | A new vendor in the trust path | Not needed; A uses only Google IAM and Kubernetes RBAC |

### The recommendation: A, a dispatcher behind one authenticated tunnel

```
laptop (single writer)                         GKE zonal cluster, us-central1-a
+-----------------------------+   one TLS      +---------------------------------------------+
| runner ->                   |   tunnel over  | ns meridian-dispatch                        |
|   DispatcherExecutor        |   the control  |  meridian-dispatcher (SA dispatcher)        |
|   http://127.0.0.1:PORT ----+-- plane DNS -->|   create/resume/delete -> ate-api-server   |
| ledger.sqlite3 (only here)  |   endpoint,    |   POST /run -> atenet-router :8444 (mTLS,  |
+-----------------------------+   IAM + RBAC   |     header ate-target-actor) -> actor      |
                                               |   reads the actor back: golden snapshot only|
                                               | ns ate-system: Substrate v1.0.0             |
                                               | pool "system": 1 node | pool "substrate":   |
                                               |   0..2 nodes, gVisor workers                |
                                               +---------------------------------------------+
```

- **The dispatcher is a thin, stateless service**: `POST /runs` takes the zone's template
  name and the run's tar (the same archive `stage_run` builds today) and answers with the
  drafts, the actor's snapshot list as read back before deletion, the image digest, and the
  per-phase timings that `SubstrateRun` records now. It keeps nothing on disk: no volume,
  read-only root filesystem, payloads never logged. As built in step 2 it runs in its own
  namespace `meridian-dispatch` as its own ServiceAccount `dispatcher`, which the atespace
  access policy makes editor on atespace `meridian` only, and its client's verb guard
  (create, resume, get, delete; never suspend, revert, tag, proxy) refuses everything else.
- **It reaches actors the way an in-cluster client does**: an mTLS `CONNECT` to
  `atenet-router.ate-system.svc:8444` with `ate-target-actor: meridian/<actor>`, presenting
  a Pod Identity CA certificate from a projected volume (`docs/reference/api-guide.md:234-248`;
  Substrate's own `benchmarking/nighthawk-ingress` client does exactly this).
  Install with
  `--ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/meridian-dispatch/sa/dispatcher`, so it
  is the only client the router admits (the podidentity signer issues
  `spiffe://cluster.local/ns/<namespace>/sa/<service-account>`, `cmd/ate-setup/commands.md:29`).
- **The ledger stays single-writer.** The dispatcher returns results; the laptop runner
  writes ActorWoke, the findings and ActorSuspended exactly as now, in the same fixed commit
  order (docs/phase4a-summary.md, Lessons 5). Executor stays one protocol:
  `DispatcherExecutor` is the Substrate executor with the actor's life moved into the
  dispatcher, selected by `[substrate] via = "dispatcher"`.
- **The tunnel**: `kubectl port-forward -n meridian-dispatch deployment/meridian-dispatcher 0:8090`,
  started once per pass. It goes over the DNS endpoint (TLS, IAM-authorized), then the API
  server's stream to the kubelet. Concurrent runs are concurrent HTTP requests on it, not
  new processes. Google's pages on the DNS endpoint say nothing about port-forward or
  streaming (searched; not found), so **guess** that it works as it does over an IP
  endpoint; the first command after the cluster exists tests it. On kind one port-forward
  is not the limit: 1, 2 or 4 tunnels give the same 10.3 to 10.7 runs a second (section 2a).
  If port-forward over the DNS endpoint fails, the fallback is the IAP bastion (6.11).
- **The laptop's identity** needs only: IAM `container.clusters.get` and
  `container.clusters.connect` on the cluster (both in `roles/container.clusterViewer`,
  https://docs.cloud.google.com/iam/docs/roles-permissions/container), and Kubernetes RBAC `pods/portforward` on the
  dispatcher's pods in namespace `meridian-dispatch`, plus the shared token. It no longer holds a Substrate editor token at
  all, which closes phase 4a's open gap "a compromised host process with the token could
  still suspend": only the dispatcher's code holds it.

What A cost: one new image and about 700 lines of code with tests (step 2, section 2a).

## 2a. The dispatcher as built, and measured on kind (phase 4b step 2)

**What runs where.**
- Laptop: `DispatcherExecutor` (`src/meridian/sandbox/dispatcher_executor.py`) stages only
  the zone's folders, builds the archive, checks the deployed template against the
  committed file, sends one request per run, and checks the answer: archive sha256, inputs
  manifest sha256, only the golden snapshot, sandbox class. The runner appends to the
  ledger in its fixed order. **No Substrate token on the laptop** in this mode.
- Cluster: `meridian-dispatcher serve` (`src/meridian/dispatcher_serve.py`), image
  `docker/dispatcher.Dockerfile` (the agent image's pinned base and locked packages, plus
  the `kubectl-ate` binary built from go.ate.dev/substrate v1.0.0, its sha256 in the
  Deployment's annotation). Manifests: `deploy/dispatcher/dispatcher.yaml`, rendered by
  `src/meridian/sandbox/dispatcher_deploy.py`: namespace `meridian-dispatch` (labelled
  `ate.dev/api-access=true`, which ate-api-server's NetworkPolicy admits), a default-deny
  ingress NetworkPolicy, ServiceAccount `dispatcher`, RBAC to read the WorkerPool and
  ClusterTrustBundles, read-only root filesystem, non-root, no capabilities, in-memory
  `/tmp`.
- Mutual authentication (`src/meridian/sandbox/dispatch.py`): a 32-byte random token, on
  the laptop in `~/.config/meridian/dispatcher.token` (0600, refused if group- or
  world-readable; outside the repository), in the cluster in a Secret mounted read-only for
  uid 1000. The runner sends it as a bearer token with a fresh nonce; the dispatcher answers
  with an HMAC-SHA256 of the nonce and the response body under the token, and the runner
  refuses any answer without a valid proof. The token is never logged or put on a command
  line (`kubectl apply -f -` from stdin).
- Timeouts: request timeout plus the 120 s placement wait plus 30 s per request; the
  dispatcher waits up to 120 s for a free worker (ResourceExhausted, backoff). A dropped
  tunnel is restarted once and the request retried; the dispatcher deletes any actor of the
  same name first, so a retry is safe.
- A lost worker (Spot preemption) is reported as `worker_lost`, raised on the laptop as
  `WorkerLostError`, and the runner retries the run **once on a new actor**. Both attempts
  are in the ledger: the lost one as an ActorWoke without ActorSuspended
  (tests/unit/test_preemption.py, simulated).
- On kind only: `meridian dispatcher deploy` adds the dispatcher's SPIFFE ID to
  atenet-router's existing allow-list (keeping `ate-system/atenet-client` for the direct
  executor) and lists both principals in the access policy. On GKE only the dispatcher's
  ID and principal (6.7, 6.8).

**Proofs on kind** (dispatcher image `localhost:5001/meridian-dispatcher@sha256:58d1ba25...`,
built from bf0dc78, clean):

| Proof | Result |
|---|---|
| C1 canary, 20 runs through the dispatcher, every zone | found in none of: the snapshot bucket (40 objects, 0.66 GB decoded), the kind node filesystem (80,236 files, 10.9 GB), host `var/` (72 files), **the dispatcher pod's filesystem** (11,256 files, 637 MB, streamed with tar), **the dispatcher's logs**. Positive controls: the canary planted in the pod's `/tmp` is found; a suspended canary actor's snapshot is found, and gone after deletion. Bucket layout: 5 golden tag snapshots, nothing outside `tags/`. `var/substrate/canary-dispatcher.json` |
| C2, 12 pairs byte-identical to OpenShell | 12 of 12 identical (Substrate 417 to 610 ms, OpenShell 4.2 to 4.6 s) |
| C3 no network from actors | 4 zones pass; every egress refused, **including a new probe from an actor to the dispatcher pod** (`egress_dispatcher`), with atenet-egress lines; controls allowed |
| C4 zone folders only | CI test per zone (staging unchanged), and through the dispatcher: the actor received exactly `meta`, `blinded`, `ops` for rbqm (tests/unit/test_dispatcher.py) |
| Unauthenticated requests | live: no header, a wrong token, a random token: 401 on `GET /v1/actors`, `POST /v1/runs`, `DELETE /v1/actors/x`, nothing run, one log line each without a body; `/healthz` 200 |
| Suspend, revert, tag, proxy | refused by the dispatcher's guard inside its pod before anything is sent; Substrate refuses its identity in another atespace (PermissionDenied) |
| Audit reviewer | 1.2.0 through the dispatcher reviewed seq 1 to 12130 (including 1,820 dispatched runs): the same 7 phase 1 findings, skipped as duplicates, nothing about any dispatched run |
| Ledger | verifies, 12,133 events |
| Substrate survives a full stop (the kind analogue of park) | every ate-system Deployment, Postgres, the dispatcher and the WorkerPool to 0 and back in 24 s: Postgres and rustfs claims (same uids and volumes), the 5 golden tags and the templates identical; 5 dispatched runs then 458 to 576 ms |

**Measurements on kind, against phase 4a** (four zones, every cut, `agents run --rerun
--all-cuts`, 910 runs each; per run from ActorWoke started_at to ActorSuspended):

| | 4a direct, 12 workers | Dispatcher, 12 workers | Dispatcher, 16 workers |
|---|---:|---:|---:|
| Wall-clock | 198.4 s | **81.3 s** | 84.3 s |
| Failed | 0 | 0 | 0 |
| p50 / p95 / max per run | 2.56 / 3.07 / 3.41 s | **1.12 / 1.33 / 1.94 s** | 1.59 / 1.95 / 2.28 s |
| Lowest host MemAvailable | 1.96 GB | 6.58 GB | 5.81 GB |
| Seq | 6629 to 8448 | 8491 to 10310 | 10311 to 12130 |

One run alone (5 sequential, warm): 425 to 924 ms, against 1.20 s p50 direct; the router
CONNECT costs about 45 ms and the tunnel about 40 ms.

**The per-run proxy is gone, and what limits throughput now.** 200-run benches:

| At a time | Runs/s | p50 | create | resume | request | delete | tunnel (p50 ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 (5 runs) | 1.6 | 557 | 26 | 215 | 61 | 101 | 42 |
| 4 | 7.3 | 540 | 30 | 192 | 94 | 112 | 40 |
| 8 | 9.9 | 800 | 48 | 272 | 146 | 173 | 63 |
| 16 | 10.4 | 1,498 | 116 | 441 | 269 | 352 | 138 |

Throughput levels off at about 10 to 11 runs a second from 8 at a time up, and every
Substrate phase (create, resume, request, delete) lengthens with concurrency. Ruled out by
experiment: the tunnel (1, 2 and 4 port-forwards give 10.7, 10.3 and 10.5 runs/s), the
dispatcher's CPU (its limit raised from 2 to 6 cores: 19.1 s against 19.4 s for 200 runs),
host CPU (50% busy of 16 threads during the bench; worker pods about 5 cores, dispatcher
about 1.9, ate-api-server about 0.5). **What limits it now is Substrate's own path on the
single kind node:** ate-api-server, Postgres and the node's one atelet doing every create,
restore and delete. **Guess:** atelet, which is per node. If so, two GKE worker nodes (two
atelets) should raise the plateau, and section 7 measures that.

## 2b. Step 3 on kind: the GKE code, exercised where kind can

Dispatcher `localhost:5001/meridian-dispatcher@sha256:090a369a...` (built from 075baa5);
agent templates unchanged (`...-49d1c5259345-m384`).

**What changed on kind.**
- The node carries `meridian.dev/pool=substrate`, and the WorkerPool names it with the
  toleration `meridian.dev/worker=true:NoSchedule`, exactly as on GKE.
- The `meridian-runner` ServiceAccount is deleted (and `var/substrate/runner.token`).
- The access policy names only the dispatcher (before: the runner and the dispatcher).
- The router admits only the dispatcher (before: `atenet-client` and the dispatcher). The
  bootstrap owner's `kubectl ate proxy` to an actor is now reset by the router.

| Proof on kind | Result |
|---|---|
| `substrate placement-check --taint-node` | pass: selector and toleration in the WorkerPool; workers Running on the labelled, tainted node; a pod without the toleration refused ("1 node(s) had untolerated taint(s)"); taint removed after |
| `substrate conformance` through the dispatcher | pass in all four zones: 6 egress targets refused (dispatcher, test net, host, peer actor, API service, metadata server), DNS refused, read-only `/opt`, controls allowed. A first run failed only on its verdict: the metadata server is refused inside the actor (no route), so there is no gateway log line for it; a line is now required only for targets whose connect reached the gateway. Both runs are in the ledger. |
| `leakcheck --env kind`, rbqm, quality, audit trail, 20 runs | pass (table below) |
| `leakcheck --env kind`, all four zones, 24 runs | pass, 80 s end to end |
| marked tests | substrate 3, openshell 22, slow 3: all pass |

Leak check sources (the second run; the first is the same within a few files):

| Source | Items | Bytes searched | Canary | Positive control |
|---|---:|---:|---|---|
| dispatcher pod filesystem | 11,267 | 637 MB | none | found (marker in its /tmp) |
| dispatcher logs | 1 | 30 kB | none | found (marker logged as a request path) |
| snapshot bucket (rustfs), decoded | 40 objects | 663 MB | none | found in all 5 golden snapshots (the worker's path in their memory) |
| node `kind-control-plane`, scan pod | 89,160 files | 12.6 GB (stored and decompressed) | none | found (marker in the scan pod's own layer) |
| laptop `var/` | 81 | 32 MB | none | found (marker file) |
| Cloud Logging | GKE only | | | unit tested against recorded gcloud answers |

Bucket layout: 40 objects, all under the 5 golden tags, none outside.

**Full passes at 12 workers, the node tainted throughout** (from the same 910 runs):

| Pass | Wall clock | p50 | p95 | Recorded |
|---|---:|---:|---:|---|
| step 2 (for comparison) | 81.3 s | 1.12 s | 1.33 s | 1,820 events |
| step 3, dispatcher from d62fb7f | 81.5 s | 1.10 s | 1.32 s | 1,820 events (new dispatcher image: new provenance) |
| step 3, three worker pods force-deleted at 10, 25, 40 s (first try) | 85.6 s | | | 908 unchanged, not recorded; 1 lost mid-request, retried and completed; 1 lost while resuming, **failed** (the gap below) |
| step 3, same, after the fix (dispatcher from 075baa5) | 84.6 s | 1.15 s | 3.13 s | all 3 lost while resuming, retried on a new actor and completed; 0 failed; 1,823 events (910 runs + 3 lost attempts) |
| step 3, plain pass after it | 83.5 s | | | 910 unchanged: **0 events** |

The gap the preemption pass found: a worker deleted while an actor restores on it fails
the resume ("actor ... crashed ... wakeup probe cancelled"). That was treated as an
ordinary failure. Any failure after the actor was created, with the actor crashed, is now
a lost worker. For a loss during resume the worker pod is unknown (resume never
answered), so the lost attempt's ActorWoke has `worker_lost` but no pod.

**Reruns (E).** A rerun is recorded only if it produced something new: a failure, a lost
worker, or provenance no recorded run of the pair has. Provenance means the executor,
image, policy, inputs manifest, drafts sha256, template, golden snapshot and dispatcher
image. Identical reruns are counted in the pass's report, and their actors are still in
`var/actors.jsonl`. Why not record every rerun: an identical rerun adds no fact a reviewer
needs; the pair's result and provenance are already in the ledger. What would need every
run is an access log of each time data was read, and that is `var/actors.jsonl` (actor,
created, deleted), not the findings ledger.

## 3. Data: what travels, how, and where it may rest

### What travels

Per run, the archive `stage_run` builds: `meta/` and only the zone's areas
(docs/threat-model.md section 4, C4). Measured on this laptop's DC-BASE: `blinded` 796 KB,
`ops` 132 KB, safety `blinded` 12 KB, the eTMF area 44 KB. So about 1 MB up per rbqm or
quality run, less for safety, and a few KB of drafts down. A full pass of 910 runs moves
**about 0.9 GB up and under 10 MB down.** The safety zone's archive holds **subject-level
safety data** (case listings by USUBJID from `data/safety_db/<cut>/blinded`); no zone
receives `unblinded`.

### In transit

1. Laptop to Google's front end: TLS to the cluster's DNS endpoint, authorized by IAM
   before the API server (substrate-gke `tools/setup-gcp/README.md:78-81`).
2. API server to the kubelet to the dispatcher pod: the port-forward stream, over the
   control plane's TLS connection to the node (**guess** from GKE's cluster trust model;
   not re-read for this plan, the first port-forward test is where to confirm it).
3. Dispatcher to atenet-router to the actor: mTLS twice (Pod Identity CA certificate to the
   router, the router to the worker's atunnel), `docs/reference/api-guide.md`,
   `benchmarking/nighthawk-ingress/README.md` (the diagram's "mTLS :443" hops).

No hop is plaintext and none uses a public address of ours. Ingress to Google Cloud is not
billed; the drafts coming back are internet egress (section 5).

### At rest

| Place | What may be there | Control and proof |
|---|---|---|
| Snapshot bucket `gs://$BUCKET_NAME` | **Only golden snapshots** (taken before any data). Layout: `atespaces/<atespace>/actors/<actor uid>/snapshots/<name>` for an actor's snapshots, `atespaces/<atespace>/tags/<tag uid>` for a tag's (`docs/reference/api-guide.md:456-486`). | (a) Runs are never suspended: the dispatcher refuses suspend, and reads each actor back before deleting it (as today). (b) **Bucket proof, mechanical:** list `gs://$BUCKET_NAME/atespaces/` recursively; every object must be under `atespaces/meridian/tags/<uid>` where `<uid>` is the UID of a tag one of the zone templates names as its golden snapshot (`kubectl ate get tag -o json`), and there must be **no object under `actors/`** once the golden builds are done. `meridian substrate bucket-check` (next step) prints both lists and fails on any other object. (c) The canary (C1) rerun on GKE: plant a unique string in 20 runs, search every object in the bucket (decoded, as on kind) and find nothing; the positive control suspends one canary actor and finds it. Afterwards the positive control's actor is deleted, its prefix collected, and (b) passes again. |
| `gs://$BUCKET_NAME-assets` | microVM guest assets only, and with `--sandbox-classes=gvisor` none at all | Install gVisor only; the bucket stays empty (the check lists it) |
| Worker node boot disks | Actor scratch while a run is live, as on kind's node; the agent image cache | Encrypted at rest by Google by default. Deleted with the node (scale to zero, teardown). Canary searches a worker node's `/var/lib/ate` and pod logs through a debug pod, as C1 did on kind. |
| Postgres volume (ate-system) | Substrate's records: actor and tag metadata, never inputs | Same as kind; checked by the canary search (`pg_dump` grep) |
| Cloud Logging | Container stdout and stderr from every pod, 30 days in `_Default` | The worker and the dispatcher must never print inputs; the canary also searches the project's logs (`gcloud logging read` for the string) and must find nothing. **Guess** until checked: the agents print only counts. An exclusion on namespace `meridian` container logs is the fallback. |
| Artifact Registry | Images only (meridian-worker, the dispatcher) | The image holds no data (C1 searched images on kind) |
| The laptop's tunnel | Nothing: a port-forward does not buffer to disk | n/a |

### What changes in docs/threat-model.md (the GKE part when the cluster exists; the dispatcher part is in it since step 2)

Section 4 gains a part "On GKE (phase 4b)":

1. **Data now leaves the laptop.** Staged inputs, including subject-level safety data,
   cross to Google Cloud in us-central1 over TLS and mTLS (section 3 above), through the
   control plane's DNS endpoint and the dispatcher. New trust: Google as processor (control
   plane, nodes, disks, logging). For a real sponsor this needs a data processing agreement
   and a check that the region suits the trial's consent and transfer terms; here the data
   is synthetic. **Accepted.**
2. **"No data at rest" gets a bucket proof** (the listing rule above) beside the canary,
   and the canary's search covers the bucket, a worker node, Postgres and Cloud Logging.
3. **The suspend guard moves into the cluster.** The laptop holds no Substrate token: it
   can only port-forward to the dispatcher. A compromised laptop process can still send a
   run, but cannot suspend, revert or tag. The remaining gap is the dispatcher's own code;
   Substrate v1.0.0 still cannot refuse an editor suspend.
4. **Who can reach actors**: on GKE atenet-router admits only
   `spiffe://cluster.local/ns/meridian-dispatch/sa/dispatcher`. On kind it admits that and
   `ate-system/atenet-client` (the `kubectl ate proxy` identity of the direct executor).
5. **Spot preemption** (section 4) can kill a worker mid-run. The actor goes to `CRASHED`
   and is deleted; it never suspends on its own, so no data snapshot results. The run
   fails and the runner retries it, as for any failed run.
6. **Snapshots in GCS** are encrypted at rest by Google (default encryption); on kind they
   were not. Still acceptable only because no data snapshot exists.
7. Known gaps adds: Cloud Logging receives whatever a container prints; the agents must
   keep printing counts only.

## 4. Minimal sizing

### What setup-gcp creates by default, and why this plan does not use it for the cluster

`bootstrap` / `create cluster` make a pool `substrate-node-pool` of **2 fixed**
`c3-standard-4` nodes (`cmd/cluster.go:126-130`, README flag table) with nested
virtualization on, plus two one-node system pools `ate-system-infra` and `ate-postgres`,
**`e2-standard-4`** each (`cmd/systempools.go:75-80`). No autoscaling, no Spot. That is four
nodes running all the time (USD 490 a month, section 5), so this plan creates the
cluster with `gcloud`, copying every setting setup-gcp applies, and uses setup-gcp for what
it does exactly and cheaply: `enable apis`, `create bucket`, `create iam`. After the cluster
exists, `setup-gcp create cluster` run against it only validates (it never recreates an
existing cluster; it updates Workload Identity, the beta APIs and Managed OpenTelemetry in
place and warns about the rest, README lines 113-116).

Settings copied from `cmd/cluster.go` and `cmd/systempools.go`:

- GKE **Standard**, version **1.37** or later on the control plane and every pool (README
  lines 125-130); a release channel that carries 1.37 (Rapid or Regular at the time of
  writing, README line 134), **not Extended** (rejected, the beta APIs below are not allowed
  there, lines 140-142).
- Beta APIs `certificates.k8s.io/v1beta1/podcertificaterequests` and
  `certificates.k8s.io/v1beta1/clustertrustbundles` (`cmd/cluster.go:38-41`).
- **Workload Identity Federation**: workload pool `$PROJECT_ID.svc.id.goog`.
- Control plane: DNS endpoint with external traffic allowed, **IP endpoints disabled**.
- Managed OpenTelemetry (collection and instrumentation components), Filestore CSI off.
- Nodes run as `ate-gke-node@$PROJECT_ID.iam.gserviceaccount.com` (holds only
  `roles/container.defaultNodeServiceAccount`), scope cloud-platform, Secure Boot and
  integrity monitoring (`cmd/systempools.go:85-95`).
- Not copied: nested virtualization (only microVMs need it; this plan installs
  `--sandbox-classes=gvisor`), the high-density sysctls (for more than about 800 sandboxes a
  node, README lines 206-220), the dashboards.

### The plan's cluster

| Part | Choice | Why |
|---|---|---|
| Cluster | Zonal, `us-central1-a` | One zone: one management fee (covered by the free tier credit, section 5), no cross-zone traffic |
| Pool `system` | 1 node, `c3-standard-4`, on-demand, no autoscaling | Holds ate-system (API server x2, controller, idp x2, router, egress, podcert controller, credential provider), the bundled Postgres, kube-system and the dispatcher. Postgres requests 2 vCPU and 1 GiB (`manifests/ate-install/postgres/postgres.yaml:213-219`), atenet-egress 200m and 512 MiB; nothing else in the install requests much (`atelet` 50m). A 2-vCPU machine cannot hold Postgres, and Google's minimum is `c3-standard-4` anyway. Not Spot: losing it stops Postgres and the API mid-pass. Only one pool for the system, not setup-gcp's two: the system components then run untainted on it, and the worker pool is the one tainted. |
| Pool `substrate` | `c3-standard-4`, **Spot**, autoscaling **0 to 2**, taint `meridian.dev/worker=true:NoSchedule`, label `meridian.dev/pool=substrate`, auto-upgrade **off** | The WorkerPool's template gets the matching `nodeSelector` and `tolerations` (both are WorkerPool fields, `manifests/ate-install/generated/ate.dev_workerpools.yaml` lines 364 and 440), so only workers land there and the pool empties when the WorkerPool is scaled to 0. 12 workers at 250m and 512 MiB (today's size) fit one 4 vCPU, 16 GB node. `c3-standard-4` is the smallest Google allows (section 1), and on Spot the cheapest of the allowed series (section 5). |
| Boot disks | `pd-balanced`, 30 GB on the worker pool, 50 GB on the system pool (C3 takes pd-balanced, not pd-standard) | Google's Substrate design page defaults to a 100 GiB hyperdisk-balanced boot disk; the agent image and gVisor need far less. **Guess** that 30 GB holds the image cache; kind's node used 10.7 GB in all. |
| Postgres volume | **Pre-create** the claim `data-postgres-0` in `ate-system` at 10 GiB before installing | The bundled StatefulSet asks for **500 GiB** (`postgres.yaml:247-258`) and ate-setup has no flag for its size (only `--ateapi-postgres-storage-class`). A StatefulSet adopts an existing claim with the template's name (Kubernetes behaviour), so a 10 GiB claim made first is the one it uses. |
| Snapshot bucket | `gs://$BUCKET_NAME`, Standard class, `us-central1`, uniform access, public access prevention on; plus the empty `-assets` bucket setup-gcp makes beside it | Golden snapshots only: 0.66 GB on kind for all templates and versions so far (docs/substrate-eval.md C1 table) |
| Images | Artifact Registry Docker repository `meridian` in `us-central1` | meridian-worker and the dispatcher. Substrate's own images come from its release registry `ghcr.io/agent-substrate/substrate` at tag v1.0.0 (`ate-setup --image-repo/--image-tag`, `cmd/ate-setup/README.md:10-40`), nothing to build. |
| Network | The `default` VPC; nodes with external IPs (GKE's default) so they can pull from ghcr.io | No Cloud NAT to pay for. Nodes accept no inbound traffic but GKE's own (firewall defaults); see Risks for private nodes. |

**Spot where safe (decided: yes for workers).** The worker pool: actors are never
suspended and hold nothing between runs, so a preemption only fails the run in progress,
which the runner retries once on a new actor (section 2a), and its actor is deleted. The
system pool stays on-demand. That
contradicts setup-gcp's warning "do not use spot or preemptible nodes" for workers
(README lines 144-170), and Google's design page says "avoid using Spot VMs for stateful
WorkerPool nodes" (15 seconds to 2 minutes of notice). Both are about actors that **keep
state** and go `CRASHED`; ours never do.

**Scaling to zero.** The cluster autoscaler removes the worker nodes only when no worker pod
is left, so the pass scripts the WorkerPool: scale it to 12 before a pass (nodes come up in
about a minute or two, **guess** until measured), back to 0 after, and the autoscaler
removes the nodes after its scale-down delay. atelet is a DaemonSet and does not hold a
node. The system node stays while a session lasts. **Between sessions the cluster is
parked** (decided): `deploy/gke/park.sh` takes the WorkerPool, the worker pool and the system
pool to 0, keeps the control plane, the Postgres claim, the bucket and the images, and
`deploy/gke/resume.sh` brings the system node back and checks that the Postgres claim and
the golden tags are the same ones (6.10). Full teardown at the end of phase 4b, or before an
idle week.

## 5. Cost

Prices for us-central1, read on **13 Nov 2026** (no page shows a last-updated date), 730
hours a month. Pages:

- Compute Engine, general purpose (on-demand): https://cloud.google.com/products/compute/pricing/general-purpose
- Spot VMs: https://cloud.google.com/spot-vms/pricing
- GKE (management fee, free tier): https://cloud.google.com/kubernetes-engine/pricing
- Disks: https://cloud.google.com/compute/disks-image-pricing
- Cloud Storage: https://cloud.google.com/storage/pricing
- Artifact Registry: https://cloud.google.com/artifact-registry/pricing
- Logging and Monitoring: https://cloud.google.com/stackdriver/pricing
- Network (egress, external IPs): https://cloud.google.com/vpc/network-pricing
- Billing budgets: https://cloud.google.com/billing/v1/pricing ("free to use")

### Unit prices used

| Item | Price |
|---|---|
| GKE management fee | USD 0.10 per cluster-hour; free tier **USD 74.40 a month per billing account**, "equivalent to one zonal Standard or Autopilot cluster", so a single zonal cluster's fee is covered (if no other cluster on the billing account uses the credit) |
| `c3-standard-4` (4 vCPU, 16 GB) | on-demand 0.201608/h, **Spot 0.071592/h** |
| Comparison: `n2-standard-4` | 0.194236 / Spot 0.116512; `n4-standard-4` 0.1814 / 0.107048 (Hyperdisk only); `e2-standard-4` 0.134023 (not supported) |
| External IPv4 on a node | 0.005/h; 0.0025/h on Spot |
| pd-balanced | 0.000136986 per GiB-hour (about 0.10 per GiB-month) |
| Cloud Storage Standard | about 0.02 per GiB-month; Always Free 5 GB-month across us-west1, us-central1, us-east1, 5,000 Class A and 50,000 Class B operations |
| Artifact Registry | first 0.5 GiB-month free, then about 0.10 per GiB-month; pulls in the same region free |
| Cloud Logging | 0.50 per GiB ingested after 50 GiB a project a month free, 30 days' retention included; GKE system metrics free |
| Internet egress (Premium tier, to North America) | first 1 GiB a month free, then 0.12 per GiB; ingress free |

### What the plan costs (USD)

Hourly lines: system node `c3-standard-4` on-demand + its IP + 50 GiB boot disk + the 10 GiB
Postgres volume = **0.2148/h**. One worker node `c3-standard-4` Spot + IP + 30 GiB boot
disk = **0.0782/h** (0.2107/h on-demand).

| State | Per hour | Per day | Per month (730 h) |
|---|---:|---:|---:|
| **Running a pass**, one Spot worker node | 0.293 | | |
| Running, two Spot worker nodes | 0.371 | | |
| Running, one on-demand worker node | 0.426 | | |
| **Idle, cluster up** (worker pool at 0) | 0.215 | **5.16** | **156.82** |
| Same, if the free tier credit were already used elsewhere | 0.315 | 7.56 | 229.82 |
| Parked: system pool resized to 0 too (nothing runs; Postgres volume, buckets, images kept) | 0.0014 | 0.03 | about 1.00 |
| **After teardown** | **0** | **0** | **0** |
| For comparison: setup-gcp's defaults (2 x c3-standard-4 + 2 x e2-standard-4, fixed) | 0.671 | 16.11 | 490.02 |
| The trap: Postgres's default 500 GiB claim alone | 0.0685 | 1.64 | 50.00 |

Lines that round to zero at this size, and why:

| Item | Estimate | Basis |
|---|---|---|
| Management fee | 0 (credit) | one zonal cluster |
| Snapshot bucket | 0 | golden snapshots about 0.7 GB (kind: 0.66 GB for every template version so far), inside the 5 GB free tier; one Class B read per restore, 910 a pass, inside 50,000 |
| Artifact Registry | 0 to 0.01 a month | meridian image 564 MB uncompressed, **guess** 0.2 to 0.3 GB compressed, plus the dispatcher; around the 0.5 GiB free tier. Substrate's images come from ghcr.io (ingress, free) |
| Logging | 0 | **guess** well under 50 GiB a month at this volume |
| Monitoring | 0 to cents | GKE system metrics free; Managed OpenTelemetry has no separate price on the page; Managed Prometheus 0.06 per million samples (**guess** under 1 a month) |
| Egress | 0 | drafts under 10 MB a pass, inside the first free GiB; inputs go up (ingress, free) |
| Budget | 0 | free |

### One full pass (910 runs)

kind ran 910 runs in 198.4 s at 12 at a time. **Guess** for GKE until measured: about 5
minutes of runs, plus about 3 minutes for a worker node to come up from 0 and pull the
image, plus the autoscaler's 10-minute scale-down delay (Google's troubleshooting page for
scale-down), so about 25 minutes of one Spot worker node:

| | Cost |
|---|---:|
| Incremental cost of a pass on a cluster that is already up (25 min of one Spot worker) | **0.033** (0.088 with an on-demand worker) |
| Per run, incremental | **0.00004** |
| A whole session: create, one pass, measure, teardown, about 2 hours with the system node | **about 0.46** (0.0005 a run) |

The budget of USD 30 a month is about **140 hours** of the idle cluster. Decided: park
between sessions (about USD 0.03 a day, about USD 1 a month), teardown at the end of the
phase. On kind the dispatched pass took 81 s, so the 5-minute guess above is generous; the
cold start from 0 nodes and the scale-down delay dominate a pass's cost on GKE.

## 6. Commands, in order

Each block assumes `source deploy/gke/env.sh` (from `deploy/gke/env.sh.example`) and an
interactive `gcloud auth login` plus `gcloud auth application-default login` (setup-gcp uses
the client libraries, README line 33). Nothing here prints a credential. setup-gcp runs
from the substrate-gke checkout at v1.0.0-gke.0 and needs Go (installed, go1.27.0).

### 6.0 The account check (done, section 1)

Rerun before creating anything, to see the versions of that day:

```bash
source deploy/gke/env.sh
gcloud container get-server-config --location="$REGION" --project="$PROJECT_ID" \
  --flatten=channels --filter='channels.channel=REGULAR' --format='yaml(channels)'
```
Success: Regular lists the `CLUSTER_VERSION` of env.sh (1.37.1-gke.1552000 on 13 Nov
2026), or a newer 1.37 patch (then update env.sh). **Stop if Regular carries no 1.37.**

### 6.1 Budget, first, so alerts exist before anything bills

```bash
gcloud services enable billingbudgets.googleapis.com --project="$PROJECT_ID"
gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
  --display-name="$BUDGET_NAME" \
  --filter-projects="projects/$PROJECT_ID" \
  --budget-amount=30USD --calendar-period=month \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
  --filter="displayName=$BUDGET_NAME" --format='yaml(displayName,amount,thresholdRules)'
```
`BILLING_ACCOUNT` is `01DC09-5AAF08-94EFA4`, the project's billing account (section 1);
confirm it is the one you meant. Success: the listing shows USD 30 and three thresholds
0.5, 0.9, 1.0. Alerts go by email
to the billing account's administrators. A budget never stops spending.

### 6.2 APIs (setup-gcp's list, `cmd/api.go:35-46`)

```bash
cd ~/src/substrate-gke
go -C tools/setup-gcp run . enable apis --project-id="$PROJECT_ID" --region="$REGION"
gcloud services list --enabled --project="$PROJECT_ID" --format='value(config.name)' \
  | grep -E '^(container|artifactregistry|iam|iamcredentials|storage|logging|monitoring)\.'
```
Success: all seven listed (plus cloudresourcemanager, networkconnectivity, serviceusage,
cloudtrace, telemetry).

### 6.3 The node service account and Artifact Registry

```bash
NODE_SA="$NODE_GSA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
gcloud iam service-accounts create "$NODE_GSA_NAME" --project="$PROJECT_ID" \
  --display-name="Substrate GKE nodes"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$NODE_SA" --role=roles/container.defaultNodeServiceAccount \
  --condition=None
gcloud artifacts repositories create "$AR_REPO" --project="$PROJECT_ID" \
  --location="$REGION" --repository-format=docker --description="meridian images"
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --project="$PROJECT_ID" \
  --location="$REGION" --member="serviceAccount:$NODE_SA" --role=roles/artifactregistry.reader
```
Success: `gcloud artifacts repositories describe "$AR_REPO" --location="$REGION"` shows
format DOCKER in us-central1.

Pushing the images: you run `gcloud auth configure-docker` yourself (decided; it edits
`~/.docker/config.json`, which I do not change), then push the two images by their
committed tags:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev            # you, once
AR="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO"
for img in meridian-agent:0.1.0 meridian-dispatcher:0.1.0; do
  docker tag "$img" "$AR/$img" && docker push "$AR/$img"
done
gcloud artifacts docker images list "$AR" --include-tags --format='table(package,tags,version)'
```
Success: both images listed; the agent image's digest is the one the GKE templates will
name (6.8 renders them for it).

### 6.4 The cluster (Standard, zonal, setup-gcp's settings) and the system pool

```bash
gcloud container clusters create "$CLUSTER_NAME" --project="$PROJECT_ID" \
  --location="$ZONE" --release-channel="$RELEASE_CHANNEL" --cluster-version="$CLUSTER_VERSION" \
  --workload-pool="$PROJECT_ID.svc.id.goog" --enable-dataplane-v2 \
  --enable-kubernetes-unstable-apis=certificates.k8s.io/v1beta1/podcertificaterequests,certificates.k8s.io/v1beta1/clustertrustbundles \
  --enable-dns-access --no-enable-ip-access \
  --no-enable-basic-auth --no-issue-client-certificate \
  --node-pool-name="$SYSTEM_POOL" --num-nodes=1 --machine-type="$SYSTEM_MACHINE_TYPE" \
  --disk-type=pd-balanced --disk-size=50 \
  --service-account="$NODE_SA" --scopes=cloud-platform \
  --shielded-secure-boot --shielded-integrity-monitoring \
  --logging=SYSTEM,WORKLOAD --monitoring=SYSTEM
gcloud container clusters describe "$CLUSTER_NAME" --location="$ZONE" \
  --format='yaml(currentMasterVersion,workloadIdentityConfig,controlPlaneEndpointsConfig,releaseChannel,enableK8sBetaApis,networkConfig.datapathProvider)'
gcloud container clusters get-credentials "$CLUSTER_NAME" --location="$ZONE" --dns-endpoint
kubectl get nodes -o wide
```
Success: version 1.37.1-gke.1552000 (not Regular's 1.36 default), workload pool `$PROJECT_ID.svc.id.goog`, datapath `ADVANCED_DATAPATH`
(Dataplane V2, required by Google's install guide), IP endpoints disabled,
both beta APIs listed, one node Ready. `kubectl` reaches the API only through the DNS
endpoint. (Managed OpenTelemetry is turned on in 6.7 by setup-gcp's own validator.)

### 6.5 The Substrate worker pool: Spot, 0 to 2, tainted, auto-upgrade off

```bash
gcloud container node-pools create "$WORKER_POOL" --cluster="$CLUSTER_NAME" \
  --location="$ZONE" --project="$PROJECT_ID" \
  --machine-type="$WORKER_MACHINE_TYPE" --spot \
  --enable-autoscaling --min-nodes=0 --max-nodes="$WORKER_MAX_NODES" --num-nodes=0 \
  --node-taints=meridian.dev/worker=true:NoSchedule --node-labels=meridian.dev/pool=substrate \
  --disk-type=pd-balanced --disk-size=30 \
  --service-account="$NODE_SA" --scopes=cloud-platform \
  --shielded-secure-boot --shielded-integrity-monitoring \
  --no-enable-autoupgrade
gcloud container node-pools describe "$WORKER_POOL" --cluster="$CLUSTER_NAME" --location="$ZONE" \
  --format='yaml(autoscaling,config.spot,config.taints,management)'
```
Success: autoscaling 0 to 2, spot true, the taint, `autoUpgrade: false`. Google allows
this on a release-channel cluster but recommends maintenance exclusions instead
(node-auto-upgrades page); the pool needs no state across upgrades, so either works.

### 6.6 Workload Identity, the bucket and IAM (setup-gcp)

```bash
cd ~/src/substrate-gke
export PROJECT_ID PROJECT_NUMBER BUCKET_NAME NODE_GSA_NAME SNAPSHOT_TRANSFER_GSA_NAME
go -C tools/setup-gcp run . create bucket --project-id="$PROJECT_ID" --region="$REGION" --name="$BUCKET_NAME"
go -C tools/setup-gcp run . create iam --project-id="$PROJECT_ID" --region="$REGION" \
  --bucket="$BUCKET_NAME" --atelet-snapshot-access=false
gcloud storage buckets describe "gs://$BUCKET_NAME" \
  --format='yaml(location,storage_class,uniform_bucket_level_access,public_access_prevention)'
gcloud storage buckets get-iam-policy "gs://$BUCKET_NAME" --format=json
```
Success: both buckets in US-CENTRAL1; on the snapshot bucket, `ate-api-server`'s Workload
Identity principal and the `ate-snapshot-transfer` service account hold objectAdmin and
bucketViewer, and **atelet holds nothing** (`--atelet-snapshot-access=false`: v1.0.0
moves snapshots on per-actor tokens, README lines 377 and 422). `create iam` also grants
atelet `artifactregistry.reader` on the Substrate image repository it is told about; with
images from ghcr.io there is none, so grant atelet read on `$AR_REPO` by hand (README
lines 449-451):

```bash
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
  --member="principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/ate-system/sa/atelet" \
  --role=roles/artifactregistry.reader
```

### 6.7 Validate the cluster with setup-gcp, pre-create the Postgres claim, install Substrate

```bash
go -C tools/setup-gcp run . create cluster --project-id="$PROJECT_ID" --region="$REGION" \
  --name="$CLUSTER_NAME" --location="$ZONE" --enable-nested-virtualization=false \
  --separate-system-nodes=false
```
Success: no error; it reports the existing cluster, enables Managed OpenTelemetry, and may
warn about sysctls and nested virtualization (expected, not needed for gVisor at this
density). It must not stop on the version check.

```bash
kubectl create namespace ate-system
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: data-postgres-0, namespace: ate-system}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard-rwo
  resources: {requests: {storage: 10Gi}}
EOF
cd ~/src/substrate   # at v1.0.0
go run ./cmd/ate-setup deploy ate-system \
  --gcp-project-id="$PROJECT_ID" \
  --storage-bucket-name="$BUCKET_NAME" --snapshot-location="gs://$BUCKET_NAME/" \
  --snapshot-transfer-gsa-name="$SNAPSHOT_TRANSFER_GSA_NAME" \
  --credential-provider='{"name":"k8s.io"}' \
  --ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/meridian-dispatch/sa/dispatcher \
  --sandbox-classes=gvisor \
  --image-repo=ghcr.io/agent-substrate/substrate --image-tag=v1.0.0
kubectl get pvc -n ate-system data-postgres-0 -o jsonpath='{.status.capacity.storage}{"\n"}'
kubectl get pods -n ate-system -o wide
```
Success: the claim reads `10Gi` (not 500Gi), every ate-system pod Running on the system
node, none on a worker node. (Flags from `cmd/ate-setup/internal/config/setting.go:100-157` and
`commands.md`; ate-setup prints a run report of every setting and its source, read it
before saying yes. **Guess** that these are the full set for gVisor on GKE.)

### 6.8 Meridian's side: templates, WorkerPool, dispatcher (code since step 3)

`deploy/gke/deploy.sh` (docs/gke-session.md step 13). Through `deploy/gke/run.sh`, which
sets `MERIDIAN_CONFIG_OVERLAY=deploy/gke/meridian.toml` (`[substrate] target = "gke"`):

- images by digest from Artifact Registry: the digest docker recorded when you pushed must
  be the one Artifact Registry holds for the tag (`resolve_pushed`); nothing is pushed by me;
- the WorkerPool's `nodeSelector` `meridian.dev/pool: substrate` and toleration
  `meridian.dev/worker=true:NoSchedule` (exercised on kind by `substrate placement-check
  --taint-node`);
- the access policy with the dispatcher as its only member; the router's allow-list checked
  to be exactly the dispatcher (set at install, 6.7);
- the dispatcher's Workload Identity: its principal holds no role; deploy reads the project,
  bucket, repository and service account IAM policies and refuses if any grant names it.

### 6.9 The leak check before the safety zone (code since step 3)

```bash
deploy/gke/run.sh leakcheck --env gke --zone rbqm --zone quality --zone quality.audit_trail
deploy/gke/run.sh leakcheck --env gke          # then with the safety zone
```
Sources and their positive controls: meridian/sandbox/leakcheck.py and
docs/gke-session.md steps 15 and 16. The check fails if the canary is found anywhere or if
any source's positive control is not.

### 6.10 Between sessions: park and resume

```bash
deploy/gke/park.sh                # WorkerPool, worker pool and system pool to 0; bastion stopped; then the check
deploy/gke/park.sh --check        # what still bills while parked
WORKERS=12 deploy/gke/resume.sh   # system node back, Substrate's state compared, workers for a pass
```
Success, park: the check shows the cluster with 0 nodes, no running instance (a bastion,
if any, TERMINATED), the 10 GiB Postgres disk unattached, the bucket and the repository,
and exits 0.

Success, resume:
- "Postgres claim: same" and "golden tags: same", compared with what park recorded in
  `var/gke/parked.json` (the uid, volume and size of `data-postgres-0`, and the golden
  tag uids);
- every ate-system pod and the dispatcher Ready.

On kind the analogue (everything scaled to 0 and back) kept both claims and all golden
tags (section 2a). On GKE the node goes too, so atelet restarts with it; resume.sh waits
for that.

### 6.11 Fallback only: IAP through a bastion with no public IP

Use this only if the port-forward over the DNS endpoint fails (6.8). The dispatcher then
gets an internal passthrough load balancer in the VPC, reachable only from the bastion,
and the laptop reaches the bastion through IAP. The bastion is a VM, not a GKE node, so
the E2 rule does not apply. **Guess** at the cost while up: about USD 0.01 an hour for the
VM and about 0.025 an hour for the forwarding rule; neither was on the pricing pages read
for section 5. park.sh stops the bastion; teardown.sh deletes it with its firewall rule
(the load balancer goes with the cluster).

```bash
gcloud compute firewall-rules create allow-iap-ssh-bastion --project="$PROJECT_ID" \
  --network=default --direction=INGRESS --action=allow --rules=tcp:22 \
  --source-ranges=35.235.240.0/20 --target-tags=iap-bastion
gcloud compute instances create "$BASTION_NAME" --project="$PROJECT_ID" --zone="$ZONE" \
  --machine-type=e2-micro --no-address --tags=iap-bastion \
  --shielded-secure-boot --shielded-integrity-monitoring --boot-disk-size=10GB
kubectl -n meridian-dispatch expose deployment meridian-dispatcher --name=dispatcher-ilb \
  --type=LoadBalancer --port=8090 \
  --overrides='{"metadata":{"annotations":{"networking.gke.io/load-balancer-type":"Internal"}}}'
# allow the bastion's internal IP through the namespace's default-deny ingress policy, then:
gcloud compute ssh "$BASTION_NAME" --zone="$ZONE" --tunnel-through-iap -- \
  -N -L 18090:"$(kubectl -n meridian-dispatch get svc dispatcher-ilb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')":8090
```
Success: `curl -s http://127.0.0.1:18090/healthz` answers `"ok"` on the laptop, and an
unauthenticated `curl http://127.0.0.1:18090/v1/actors` answers 401. The executor then
uses `127.0.0.1:18090` instead of the port-forward (a setting the next step adds).

### 6.12 Teardown, one script, then the check

```bash
deploy/gke/teardown.sh          # asks for the project id, deletes, then checks
deploy/gke/teardown.sh --check  # any time: lists what is left, changes nothing
```
Success: the check exits 0 and prints `none` for each of these:
- clusters, and instances including the bastion;
- disks, snapshots and images;
- addresses, forwarding rules and routers;
- buckets and repositories.

The next day's billing report (by SKU) shows no charge after the teardown hour. All three
scripts share `deploy/gke/lib.sh`. Their checks were exercised against a stubbed `gcloud`
in three states (parked, a node running, everything gone): park's check passes only with
no running node, and teardown's only with nothing left.

## 7. Measurement plan against kind

Same workload and the same commands as phase 4a, so the numbers line up with
docs/phase4a-summary.md and docs/substrate-eval.md.

| Measure | How | kind baseline |
|---|---|---|
| One run, p50 and p95 | 100 sequential `rbqm.kri_monitor` runs on DC-BASE (`meridian substrate bench`), warm worker node | dispatcher on kind: about 0.5 s (direct 1.20 / 1.30 s) |
| Per-phase breakdown | The dispatcher's timings per phase (tunnel request, create, resume, POST /run, read-back, delete), plus the laptop's round trip; tells WAN from Substrate | resume 225 ms p50 on kind |
| Concurrency | 200 runs at 1, 4, 8, 12, 16, 24, 32 at a time; p50, p95 and runs per second at each; **one worker node then two** (tests the atelet guess) | dispatcher on kind: 10 to 11 runs/s from 8 up (section 2a) |
| Full pass | `agents run --rerun --all-cuts` on the four zones, 12 at a time, then the best concurrency from the sweep; wall-clock, failed runs, p50 and p95 | dispatcher on kind: 81.3 s at 12 (p50 1.12, p95 1.33 s), 84.3 s at 16; direct 4a 198.4 s |
| Cold start | First run after scaling the WorkerPool from 0: node provisioning, image pull, first restore from GCS | n/a on kind |
| Determinism | The full pass's ledger compared with kind's (same findings, same drafts per run, C2) | 12 of 12 drafts byte-identical |
| Cost per run | (worker node-hours + system node-hours + disks + egress over the pass's window) / runs, from the billing export's SKU lines for that day; the estimate in section 5 is the prediction | n/a |
| Tunnel limit | Runs per second against the number of port-forwards (1, 2, 4) at the best concurrency; over the WAN it may matter where on kind it did not | kind: none (10.7, 10.3, 10.5 runs/s) |
| Preemption | Delete a worker node mid-pass (or wait for a real preemption): the runs on it fail once and succeed on retry, the ledger shows the lost attempt | simulated in tests only |

Done when: the full pass on GKE gives the same ledger findings as kind, the bucket proof and
the canary pass, and the numbers above are in docs/substrate-eval.md with the day's bill.

## 8. Risks

1. **Spending past the budget.** A budget alerts; it does not stop anything. Left up, the
   cluster costs more than USD 30 a month (section 5). Mitigation: teardown at the end of
   every session, the 50/90/100 percent alerts, and `deploy/gke/teardown.sh --check`.
2. **Orphaned disks.** Deleting a cluster does not delete disks behind PersistentVolumes;
   the teardown deletes the claims first and then sweeps leftover `pvc-` disks, and the
   check lists every disk in the project.
3. **Spot preemption mid-pass** (worker pool). Runs fail and are retried; a pass takes
   longer. If Spot capacity for the machine type is short in the zone, the pool stays at 0
   and runs wait: fall back to on-demand for that pass.
4. **Auto-upgrade and auto-repair** recreate nodes on Google's schedule. Auto-upgrade is off
   on the worker pool (setup-gcp's warning; Google allows it but prefers maintenance
   exclusions, "no minor or node upgrades" up to end of support). Auto-repair can be
   turned off (`--no-enable-autorepair`); the plan leaves it on and treats a repair like a
   preemption.
5. **Version drift.** A channel moves past 1.37; Substrate v1.0.0 supports 1.37 and higher,
   but a much newer minor is untested by us. The control plane auto-upgrades; pin with a
   maintenance exclusion if a pass is due.
6. **The port-forward tunnel** may drop (idle timeouts, a control plane upgrade) or cap
   throughput. The executor reconnects and retries a request whose answer it did not get;
   a retried run is a new actor, so a half-finished run leaves nothing but a deleted actor.
   As built, the dispatcher deletes any actor of that name before a run, so "same run
   twice" is safe; the fallback for a tunnel that does not work at all is 6.11.
7. **The 500 GiB Postgres claim** if the pre-created claim is missed: about USD 50 a month
   on `pd-balanced` (section 5). The teardown check catches it; the install step checks the
   claim's size before going on.
8. **ghcr.io pulls** depend on GitHub's registry and on nodes having internet access. With
   private nodes this needs Cloud NAT or an Artifact Registry remote repository (substrate-gke
   has a branch about remote mirrors; not read for this plan).
9. **Logging**: a container that prints inputs puts them in Cloud Logging for 30 days. The
   canary's log search is the check.
10. **Upstream churn**: substrate-gke is days old with one tag; its README describes main, not
    always v1.0.0-gke.0 (it already names flags such as `--cluster-size` that Substrate
    v1.0.0's ate-setup also has). Every command here was read from the tagged sources.
11. **E2 machines** are what setup-gcp defaults to for its system pools, while Google's guide
    says E2 cannot be used; this plan uses no E2 machine anywhere.
13. **Autoscaler**: Google states the autoscaler never takes a whole Standard cluster to zero
    nodes; a single pool with `--min-nodes=0` is the documented gcloud flag, but Google's
    page has no sentence saying that pool reaches 0. The system node keeps the cluster above
    zero, and the measurement plan records whether the worker pool empties.
12. **Region and data**: us-central1 for subject-level data needs a sponsor's privacy review
    in a real trial (section 3, threat model change 1).

## Decisions (steps 2 and 3) and open questions

Decided by you after step 1:

- **Machine type:** C3 if the guide names it, and it does (section 1).
- **Spot:** for the worker pool, with one retry on a new actor; the system pool on-demand.
- **Between sessions:** park; teardown at the end of phase 4b or before an idle week.
- **Data in us-central1:** acceptable. The leak check must pass for the other three zones
  before the safety zone moves (6.9).
- **Dispatcher:** built and measured on kind first (section 2a).
- **Images:** you run `gcloud auth configure-docker` yourself; no skopeo.
- **Fallback:** the IAP bastion with no public IP, included in teardown.sh and park.sh.

Decided after step 2:

- **Billing account:** `01DC09-5AAF08-94EFA4`. **No free tier credit**: the management fee
  of USD 0.10 an hour bills whenever the cluster exists, parked included (about USD 2.43 a
  parked day, about 74 a parked month, more than the USD 30 budget). docs/gke-session.md
  has the corrected session costs; section 5's "free tier" lines no longer apply.
- **GKE code on kind first:** done in step 3 (deploy/gke/, `meridian leakcheck`,
  `meridian substrate placement-check`); the bucket and Cloud Logging searches are unit
  tested against recorded gcloud answers and run for real only on GKE.
- **The laptop's runner identity:** removed on kind in step 3. The dispatcher is the only
  member of the access policy and of atenet-router's ingress allow-list, on kind as on GKE.
  The step 4a numbers stay the recorded comparison (section 2a).

The first session's commands, in order, with their checks, times and costs:
**docs/gke-session.md** (part 1 yours, part 2 mine). Sections 6.8 and 6.9 below are now code.

Open:

1. **Park or tear down** without the credit: parking saves about an hour of setup but
   costs USD 2.43 a day; I would tear down whenever the next session is more than a day or
   two away. Your call; the scripts support both.

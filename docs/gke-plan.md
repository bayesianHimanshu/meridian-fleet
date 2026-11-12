# Phase 4b plan: the Substrate zones on GKE (us-central1)

Phase 4b step 1. This is a plan. It creates nothing in Google Cloud: every command in it is
for a person to run, in order, once the open questions at the end are settled. The rbqm,
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

**Not completed: gcloud needs you to log in again.** Read-only commands run on 13 Nov 2026:

| Command | Result |
|---|---|
| `gcloud --version` | Google Cloud SDK 539.0.0 |
| `gcloud config list` | configuration `bayesianhimanshu-org`, project **`meridian-fleet-agents`**, an organization account |
| `gcloud projects describe meridian-fleet-agents` | `Reauthentication failed. cannot prompt during non-interactive execution.` |

Every other check (billing, enabled APIs, GKE versions, machine types) needs the same
login, so section 6.0 lists them for you to run after `gcloud auth login`. Until then, what
the requirements are, from the sources:

| Requirement | Source | This plan |
|---|---|---|
| GKE **1.37 or later**, control plane and every node pool | substrate-gke README lines 125-130; Google install guide | `--cluster-version=1.37`; 6.0 checks a channel carries it |
| **Standard**, not Autopilot | Google install guide ("Autopilot is not supported") | Standard |
| Not the **Extended** channel (explicit beta APIs not allowed there) | README lines 140-142; Google install guide | Regular |
| **Workload Identity Federation** | README lines 400-402; Google install guide | `--workload-pool=$PROJECT_ID.svc.id.goog` |
| **Dataplane V2** | Google install guide | `--enable-dataplane-v2` |
| **No E2**: "machine series that run on mixed CPU architectures (such as E2 machine types)" cannot be used; no Arm | Google install guide and design page | no E2 anywhere (setup-gcp's own system-pool default is `e2-standard-4`, `cmd/systempools.go:75-80`: not used) |
| Nodes "**`c3-standard-4` or larger**"; for gVisor, x86 general-purpose or compute-optimized "such as N4, C3, C4, or N2" | Google install guide | `c3-standard-4` for both pools |
| APIs | setup-gcp `cmd/api.go:35-46` (12 APIs); Google: only the GKE API beforehand | 6.2 |

Google pages: https://docs.cloud.google.com/kubernetes-engine/ai-ml/install-overview-substrate
and https://docs.cloud.google.com/kubernetes-engine/docs/how-to/design-substrate-infrastructure
(read 13 Nov 2026; neither shows a last-updated date).

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
| runner -> SubstrateExecutor |   tunnel over  | ns meridian                                 |
|   (dispatch mode)           |   the control  |  meridian-dispatcher (SA meridian-runner)   |
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
  read-only root filesystem, payloads never logged. It runs as the existing
  `meridian/meridian-runner` ServiceAccount, so the atespace access policy
  (`deploy/substrate/access-policy.yaml`, editor on atespace `meridian` only) is unchanged,
  and the runner's verb guard (create, resume, get, delete; never suspend, revert, tag)
  moves into it.
- **It reaches actors the way an in-cluster client does**: an mTLS `CONNECT` to
  `atenet-router.ate-system.svc:8444` with `ate-target-actor: meridian/<actor>`, presenting
  a Pod Identity CA certificate from a projected volume (`docs/reference/api-guide.md:234-248`;
  Substrate's own `benchmarking/nighthawk-ingress` client does exactly this). Install with
  `--ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/meridian/sa/meridian-runner`, so it
  is the only client the router admits (the podidentity signer issues
  `spiffe://cluster.local/ns/<namespace>/sa/<service-account>`, `cmd/ate-setup/commands.md:29`).
- **The ledger stays single-writer.** The dispatcher returns results; the laptop runner
  writes ActorWoke, the findings and ActorSuspended exactly as now, in the same fixed commit
  order (docs/phase4a-summary.md, Lessons 5). Executor stays one protocol: a third
  implementation, `SubstrateDispatchExecutor`, beside OpenShell and Substrate-on-kind,
  selected per zone by `[runtime] executors`.
- **The tunnel**: `kubectl port-forward -n meridian deploy/meridian-dispatcher 0:8080`,
  started once per pass. It goes over the DNS endpoint (TLS, IAM-authorized), then the API
  server's stream to the kubelet. Concurrent runs are concurrent HTTP requests on it, not
  new processes. Google's pages on the DNS endpoint say nothing about port-forward or
  streaming (searched; not found), so **guess** that it works as it does over an IP
  endpoint; the first command after the cluster exists tests it. **Guess:** one port-forward carries 12 to 32 concurrent ~1 MB requests
  without becoming the limit; the measurement plan checks it, and the fallback is a small
  pool of tunnels.
- **The laptop's identity** needs only: IAM `container.clusters.get` and
  `container.clusters.connect` on the cluster (both in `roles/container.clusterViewer`,
  https://docs.cloud.google.com/iam/docs/roles-permissions/container), and Kubernetes RBAC `pods/portforward` on the
  dispatcher's pods in namespace `meridian`. It no longer holds a Substrate editor token at
  all, which closes phase 4a's open gap "a compromised host process with the token could
  still suspend": only the dispatcher's code holds it.

What A costs: one new image and about 300 lines of code (dispatcher plus executor), which
the next step builds and tests against kind first (the dispatcher runs the same on kind,
reached by the same `kubectl port-forward`), so the GKE step only changes where it runs.

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

### What changes in docs/threat-model.md (when the cluster exists, not now)

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
4. **Who can reach actors**: atenet-router admits only
   `spiffe://cluster.local/ns/meridian/sa/meridian-runner`. On kind it admitted
   `ate-system/atenet-client` (the `kubectl ate proxy` identity).
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

**Spot where safe.** The worker pool: a run is never suspended, so a preempted worker loses
only the run in flight, which fails and is retried, and its actor is deleted. That
contradicts setup-gcp's warning "do not use spot or preemptible nodes" for workers
(README lines 144-170), and Google's design page says "avoid using Spot VMs for stateful
WorkerPool nodes" (15 seconds to 2 minutes of notice). Both are about actors that **keep
state** and go `CRASHED`; ours never do. The system pool: not safe (Postgres, API server). If it
turns out that a preemption leaves state the dispatcher cannot clean up, the worker pool
goes on-demand (section 5 gives both prices).

**Scaling to zero.** The cluster autoscaler removes the worker nodes only when no worker pod
is left, so the pass scripts the WorkerPool: scale it to 12 before a pass (nodes come up in
about a minute or two, **guess** until measured), back to 0 after, and the autoscaler
removes the nodes after its scale-down delay. atelet is a DaemonSet and does not hold a
node. The system node stays: "idle with the cluster up" is one node.

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

The budget of USD 30 a month is about **140 hours** of the idle cluster. The plan is to
create the cluster for a working session and tear it down (or park it) at the end, not to
leave it up.

## 6. Commands, in order

Each block assumes `source deploy/gke/env.sh` (from `deploy/gke/env.sh.example`) and an
interactive `gcloud auth login` plus `gcloud auth application-default login` (setup-gcp uses
the client libraries, README line 33). Nothing here prints a credential. setup-gcp runs
from the substrate-gke checkout at v1.0.0-gke.0 and needs Go (installed, go1.27.0).

### 6.0 Before anything: the read-only account check (section 1)

```bash
gcloud auth login                                   # interactive, in your terminal
gcloud auth application-default login               # setup-gcp needs ADC
source deploy/gke/env.sh
gcloud projects describe "$PROJECT_ID" --format='yaml(projectId,projectNumber,lifecycleState)'
gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled,billingAccountName)'
gcloud services list --enabled --project="$PROJECT_ID" --format='value(config.name)' | sort
gcloud container get-server-config --location="$REGION" --project="$PROJECT_ID" \
  --format='yaml(channels,validMasterVersions[0:8],validNodeVersions[0:8])'
gcloud compute machine-types list --zones="$ZONE" --project="$PROJECT_ID" \
  --filter='name~^(c3|n2|n4)-standard-4$' --format='table(name,guestCpus,memoryMb)'
```
Success: billing enabled; the Regular (or Rapid) channel lists a 1.37 version; the
machine types are offered in the zone. **Stop if no channel carries 1.37.**

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
Success: the listing shows USD 30 and three thresholds 0.5, 0.9, 1.0. Alerts go by email
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

Pushing the image changes no Docker configuration if it goes through `skopeo` with a
one-off token on stdin (**guess** that skopeo is acceptable to you; `docker push` would
need `gcloud auth configure-docker`, which edits `~/.docker/config.json`):

```bash
IMG="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/meridian-worker"
gcloud auth print-access-token | skopeo login -u oauth2accesstoken --password-stdin \
  --authfile "$XDG_RUNTIME_DIR/ar-auth.json" "$REGION-docker.pkg.dev"
skopeo copy --authfile "$XDG_RUNTIME_DIR/ar-auth.json" \
  docker-daemon:meridian-worker:<committed tag> "docker://$IMG:<same tag>"
rm -f "$XDG_RUNTIME_DIR/ar-auth.json"
gcloud artifacts docker images list "$IMG" --include-tags
```
Success: the listing shows the same `sha256:` digest as the committed template
(`deploy/substrate/templates`).

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
Success: version 1.37.x, workload pool `$PROJECT_ID.svc.id.goog`, datapath `ADVANCED_DATAPATH`
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
  --ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/meridian/sa/meridian-runner \
  --sandbox-classes=gvisor \
  --image-repo=ghcr.io/agent-substrate/substrate --image-tag=v1.0.0
kubectl get pvc -n ate-system data-postgres-0 -o jsonpath='{.status.capacity.storage}{"\n"}'
kubectl get pods -n ate-system -o wide
```
Success: the claim reads `10Gi` (not 500Gi), every ate-system pod Running on the system
node, none on a worker node. (Flags from `cmd/ate-setup/internal/config/setting.go:100-157` and
`commands.md`; ate-setup prints a run report of every setting and its source, read it
before saying yes. **Guess** that these are the full set for gVisor on GKE.)

### 6.8 Meridian's side (next step's code; listed for completeness)

`meridian substrate deploy` against the GKE context with image references in `$AR_REPO`,
the WorkerPool rendered with the worker pool's `nodeSelector`/`tolerations`, the
dispatcher Deployment, then the golden builds and `meridian substrate bucket-check`.
Success: `bucket-check` lists only `atespaces/meridian/tags/<golden uid>` objects.

### 6.9 Teardown, one script, then the check

```bash
deploy/gke/teardown.sh          # asks for the project id, deletes, then checks
deploy/gke/teardown.sh --check  # any time: lists what is left, changes nothing
```
Success: the check prints `none` for clusters, instances, disks, snapshots, images,
addresses, forwarding rules, routers, buckets and repositories, and exits 0. The next
day's billing report (by SKU) shows no charge after the teardown hour.

## 7. Measurement plan against kind

Same workload and the same commands as phase 4a, so the numbers line up with
docs/phase4a-summary.md and docs/substrate-eval.md.

| Measure | How | kind baseline |
|---|---|---|
| One run, p50 and p95 | 100 sequential `rbqm.kri_monitor` runs on DC-BASE (`meridian substrate bench`, the step 3 bench), warm worker node | 1.20 s / 1.30 s |
| Per-phase breakdown | The dispatcher's timings per phase (tunnel request, create, resume, POST /run, read-back, delete), plus the laptop's round trip; tells WAN from Substrate | resume 225 ms p50 on kind |
| Concurrency | 200 runs at 1, 4, 8, 12, 16, 24, 32 at a time; p50, p95 and runs per second at each; one worker node then two | 16 at once: 3.52 s / 5.57 s; plateau near 12 |
| Full pass | `agents run --rerun --all-cuts` on the four zones, 12 at a time, then the best concurrency from the sweep; wall-clock, failed runs, p50 and p95 | 910 runs, 0 failed, 198.4 s; p50 2.56 s, p95 3.07 s |
| Cold start | First run after scaling the WorkerPool from 0: node provisioning, image pull, first restore from GCS | n/a on kind |
| Determinism | The full pass's ledger compared with kind's (same findings, same drafts per run, C2) | 12 of 12 drafts byte-identical |
| Cost per run | (worker node-hours + system node-hours + disks + egress over the pass's window) / runs, from the billing export's SKU lines for that day; the estimate in section 5 is the prediction | n/a |
| Tunnel limit | Runs per second against the number of port-forwards (1, 2, 4) at the best concurrency | n/a |

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
   The dispatcher must make "same run id twice" safe (it deletes any actor of that run
   first).
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

## Open questions

1. **Log in to gcloud** (`! gcloud auth login` and `! gcloud auth application-default
   login`), then I run section 6.0's read-only checks and fill section 1. Which billing
   account should the budget go on, and does any other GKE cluster on it already use the
   free tier credit (that adds USD 0.10 an hour)?
2. **Machine type**: Google's guide says "c3-standard-4 or larger" and also names N2 and N4
   as series. Is `c3-standard-4` your reading too? `n2-standard-4` on-demand would save
   USD 0.007 an hour on the system node; on Spot, C3 is the cheapest.
3. **Spot on the worker pool** goes against substrate-gke's and Google's advice for
   *stateful* workers. Ours never suspend. Spot, or on-demand (0.13 an hour more while
   running)?
4. **Session or always-on**: tear down after each session (recommended; about USD 0.46 a
   session) or park the cluster with the system pool at 0 (about USD 1 a month, keeps the
   golden snapshots and Postgres)? Left up, it is USD 157 a month, five times the budget.
5. **Data**: subject-level safety data (synthetic) will be in Google's us-central1 during
   runs. Are you comfortable with that, and should the canary's Cloud Logging search be a
   gate before the safety zone moves?
6. **The dispatcher** (topology A) is new code. Build it in the next step against kind
   first, as proposed? Or start with option B (today's executor, unchanged) to measure GKE
   sooner and accept the throughput limit?
7. **Pushing images** without editing `~/.docker/config.json`: is `skopeo` acceptable (6.3),
   or do you want to run `gcloud auth configure-docker` yourself?
8. **Port-forward over the DNS endpoint** is not documented either way by Google (searched;
   not found). If it fails, the fallback is IAP TCP forwarding through a small bastion VM
   (IAP TCP forwarding is free; **guess** USD 0.01 to 0.03 an hour for a small VM; it is a VM, not a
   node, so the E2 rule does not apply) rather than Connect gateway (10 streams, sessions
   end after 20 to 30 minutes). Agreed?

# The first GKE session: one runbook

Phase 4b step 3. The exact commands, in order, for the first session on GKE: **part 1 is
yours** (everything that creates or changes a Google Cloud resource, API, IAM binding or
budget), **part 2 is mine** once the cluster exists (only Kubernetes objects, read-only
gcloud, and the leak check's reads). Each step has its success check, an estimate of its
elapsed time, and the estimated cost so far. The plan and its reasoning are in
docs/gke-plan.md; this file is what to type.

## Costs used here

From docs/gke-plan.md section 5 (us-central1 list prices read on 13 Nov 2026), with one
change: **this billing account has no free tier credit** (your answer to step 2), so the
GKE management fee of USD 0.10 an hour bills whenever the cluster exists, parked or not.

| While | USD an hour | Made of |
|---|---:|---|
| the cluster exists | 0.100 | management fee |
| plus the system node | 0.215 | `c3-standard-4` on demand, its IP, 50 GiB boot disk, 10 GiB Postgres disk |
| plus a Spot worker node | 0.078 | `c3-standard-4` Spot, its IP, 30 GiB boot disk |
| **cluster up, no worker** | **0.315** | |
| **during a pass, one Spot worker** | **0.393** | |
| **parked** (no node) | **0.101** (2.43 a day) | the fee and the Postgres disk |
| torn down | 0 | |

Internet egress to the laptop is the first GiB a month free, then 0.12 per GiB. The leak
check downloads the snapshot bucket (kind: 0.66 GB) and streams the dispatcher pod's
filesystem (kind: 0.64 GB) each time; it scans the nodes in place, so their disks never
leave Google. Budget alerts at USD 15, 27 and 30 a month (step 2).

**Park or tear down.** Without the credit, one parked day (USD 2.43) costs more money than
tearing down and recreating (about USD 0.40 of cluster time), and a month parked (about USD
74) is more than the budget. What parking saves is about an hour of part 1. Your decision
stands (park while phase 4b is active; tear down at its end or before an idle week); I would
tear down whenever the next session is more than a day or two away.

## Before the session (no cost)

```bash
cd ~/meridian-fleet
cp deploy/gke/env.sh.example deploy/gke/env.sh   # once; set PROJECT_NUMBER=932415716644
                                                 # and BILLING_ACCOUNT=01DC09-5AAF08-94EFA4
source deploy/gke/env.sh
gcloud auth login && gcloud auth application-default login   # setup-gcp uses the client libraries
gcloud config set project "$PROJECT_ID"
```

## Part 1: you (about 60 minutes, about USD 0.21 by its end)

### 1. Versions of the day (1 min, USD 0)

```bash
gcloud container get-server-config --location="$REGION" --project="$PROJECT_ID" \
  --flatten=channels --filter='channels.channel=REGULAR' --format='yaml(channels)'
```
Success: Regular lists `1.37.1-gke.1552000` (env.sh's `CLUSTER_VERSION`) or a newer 1.37
patch (then update env.sh). **Stop if Regular has no 1.37.**

### 2. Budget, before anything bills (3 min, USD 0)

```bash
gcloud services enable billingbudgets.googleapis.com --project="$PROJECT_ID"
gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
  --display-name="$BUDGET_NAME" --filter-projects="projects/$PROJECT_ID" \
  --budget-amount=30USD --calendar-period=month \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
  --filter="displayName=$BUDGET_NAME" --format='yaml(displayName,amount,thresholdRules)'
```
Success: USD 30, thresholds 0.5, 0.9 and 1.0. (A budget alerts; it never stops spending.)

### 3. APIs (3 min, USD 0)

```bash
cd ~/src/substrate-gke    # at v1.0.0-gke.0
go -C tools/setup-gcp run . enable apis --project-id="$PROJECT_ID" --region="$REGION"
gcloud services list --enabled --project="$PROJECT_ID" --format='value(config.name)' \
  | grep -cE '^(container|artifactregistry|iam|iamcredentials|storage|logging|monitoring)\.'
cd ~/meridian-fleet
```
Success: the count is 7.

### 4. The node service account and Artifact Registry (4 min, USD 0)

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
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" \
  --format='value(format,name)'
```
Success: `DOCKER projects/meridian-fleet-agents/locations/us-central1/repositories/meridian`.

### 5. configure-docker and push the two images (10 min, USD 0)

The images are the ones kind runs, built from clean commits (`meridian image build` and
`meridian dispatcher deploy` refuse a dirty tree). Pushing records their digests locally;
my deploy reads them back and checks Artifact Registry holds the same.

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev     # once; edits ~/.docker/config.json
AR="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO"
for img in meridian-agent:0.1.0 meridian-dispatcher:0.1.0; do
  docker tag "$img" "$AR/$img" && docker push "$AR/$img"
done
gcloud artifacts docker images list "$AR" --include-tags \
  --format='table(package.basename(),tags,version)'
```
Success: both images listed with tag `0.1.0` and a `sha256:` version. Storage: about
0.3 GB, inside the 0.5 GiB free tier (a guess until listed).

### 6. The cluster and its system pool (10 min; USD 0.03 by its end)

The fee and the system node start billing here.

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
  --format='yaml(currentMasterVersion,workloadIdentityConfig,releaseChannel,networkConfig.datapathProvider)'
```
Success: version 1.37.1-gke.1552000, workload pool `meridian-fleet-agents.svc.id.goog`,
datapath `ADVANCED_DATAPATH`.

### 7. The Spot worker pool, 0 to 2 nodes, labelled and tainted (3 min; USD 0.05)

```bash
gcloud container node-pools create "$WORKER_POOL" --cluster="$CLUSTER_NAME" \
  --location="$ZONE" --project="$PROJECT_ID" \
  --machine-type="$WORKER_MACHINE_TYPE" --spot \
  --enable-autoscaling --min-nodes=0 --max-nodes="$WORKER_MAX_NODES" --num-nodes=0 \
  --node-taints=meridian.dev/worker=true:NoSchedule --node-labels=meridian.dev/pool=substrate \
  --disk-type=pd-balanced --disk-size=30 \
  --service-account="$NODE_SA" --scopes=cloud-platform \
  --shielded-secure-boot --shielded-integrity-monitoring --no-enable-autoupgrade
gcloud container node-pools describe "$WORKER_POOL" --cluster="$CLUSTER_NAME" --location="$ZONE" \
  --format='yaml(autoscaling,config.spot,config.taints,config.labels,management.autoUpgrade)'
```
Success: autoscaling 0 to 2, `spot: true`, the taint and the label (the WorkerPool's
`nodeSelector` and toleration name exactly these: deploy/gke/meridian.toml, exercised on
kind), `autoUpgrade: false`.

### 8. The bucket, Workload Identity and IAM (5 min; USD 0.08)

```bash
cd ~/src/substrate-gke
export PROJECT_ID PROJECT_NUMBER BUCKET_NAME NODE_GSA_NAME SNAPSHOT_TRANSFER_GSA_NAME
go -C tools/setup-gcp run . create bucket --project-id="$PROJECT_ID" --region="$REGION" --name="$BUCKET_NAME"
go -C tools/setup-gcp run . create iam --project-id="$PROJECT_ID" --region="$REGION" \
  --bucket="$BUCKET_NAME" --atelet-snapshot-access=false
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
  --member="principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT_ID.svc.id.goog/subject/ns/ate-system/sa/atelet" \
  --role=roles/artifactregistry.reader
gcloud storage buckets describe "gs://$BUCKET_NAME" --format='value(location,public_access_prevention)'
cd ~/meridian-fleet
```
Success: `US-CENTRAL1 enforced`. **No binding for the dispatcher**: it calls no Google API,
so its Workload Identity principal holds no role; `meridian dispatcher deploy` refuses if
any IAM policy names it (step 13).

### 9. Credentials over the DNS endpoint (1 min; USD 0.09)

```bash
gcloud container clusters get-credentials "$CLUSTER_NAME" --location="$ZONE" --dns-endpoint
kubectl config current-context
kubectl get nodes -o wide
```
Success: the context is `gke_meridian-fleet-agents_us-central1-a_meridian-substrate` (the
one deploy/gke/meridian.toml names) and one node is Ready.

### 10. setup-gcp's cluster validation (5 min; USD 0.11)

```bash
cd ~/src/substrate-gke
go -C tools/setup-gcp run . create cluster --project-id="$PROJECT_ID" --region="$REGION" \
  --name="$CLUSTER_NAME" --location="$ZONE" --enable-nested-virtualization=false \
  --separate-system-nodes=false
cd ~/meridian-fleet
```
Success: it reports the existing cluster and turns on Managed OpenTelemetry; warnings about
sysctls and nested virtualization are expected. It must not stop on the version check.

### 11. The 10 GiB Postgres claim, then Substrate, the router admitting only the dispatcher (15 min; USD 0.19)

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
cd ~/src/substrate    # at v1.0.0
go run ./cmd/ate-setup deploy ate-system \
  --gcp-project-id="$PROJECT_ID" \
  --storage-bucket-name="$BUCKET_NAME" --snapshot-location="gs://$BUCKET_NAME/" \
  --snapshot-transfer-gsa-name="$SNAPSHOT_TRANSFER_GSA_NAME" \
  --credential-provider='{"name":"k8s.io"}' \
  --ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/meridian-dispatch/sa/dispatcher \
  --sandbox-classes=gvisor \
  --image-repo=ghcr.io/agent-substrate/substrate --image-tag=v1.0.0
cd ~/meridian-fleet
kubectl get pvc -n ate-system data-postgres-0 -o jsonpath='{.status.capacity.storage}{"\n"}'
kubectl get pods -n ate-system -o wide
ls ~/.cache/ate-setup/installs/
```
Success: `10Gi`; every ate-system pod Running on the system node; an install record named
`gke_meridian-fleet-agents_us-central1-a_meridian-substrate.yaml` (**guess**: ate-setup
names it after the context, as `kind-kind.yaml` on kind; if not, tell me the name and I
change `install_record` in deploy/gke/meridian.toml). Read ate-setup's run report before
saying yes; `--ingress-allowed-spiffe-ids` is the only client the router admits.

### 12. Hand over (1 min)

```bash
deploy/gke/teardown.sh --check   # read only: what exists now
```
Tell me the cluster is up. **Cost so far: about USD 0.21** (about 40 minutes of the fee and
the system node). Everything after this is part 2.

## Part 2: me, once the cluster exists (about 2 hours; about USD 1.16 by its end)

Every command goes through `deploy/gke/run.sh`, which sets
`MERIDIAN_CONFIG_OVERLAY=deploy/gke/meridian.toml` (`[substrate] target = "gke"`). Nothing
here creates a Google Cloud resource; the gcloud calls are reads.

### 13. Deploy Meridian's side (15 min; USD 0.31)

```bash
deploy/gke/deploy.sh
```
It runs, in order: `dispatcher token`; `substrate deploy` (the agent image by its Artifact
Registry digest, deploy/gke/substrate rendered, the WorkerPool on the Spot pool, the
atespace with the dispatcher as the only member of its access policy, templates, golden
snapshots: the first Spot node comes up here, about 3 minutes); `dispatcher deploy` (its
image by digest, the router checked to admit only the dispatcher, every IAM policy read to
show no grant names it); `substrate placement-check`; `dispatcher status`.
Success: the five templates have golden snapshots; placement-check passes (workers only on
labelled, tainted nodes, the dispatcher off them, a pod without the toleration refused);
`status` answers through one port-forward over the DNS endpoint. If the port-forward fails,
the fallback is the IAP bastion of docs/gke-plan.md 6.11 (yours to create).

### 14. Conformance, four zones (5 min; USD 0.34)

```bash
deploy/gke/run.sh substrate conformance
```
Success: `pass` for rbqm, safety.sae_reconciler, quality and quality.audit_trail; every
egress check refused, including the dispatcher, the API server and the metadata server
(169.254.169.254, Workload Identity's token endpoint); the controls allowed.

### 15. The leak check: rbqm, quality and the audit trail first (35 min; USD 0.61)

```bash
deploy/gke/run.sh leakcheck --env gke --zone rbqm --zone quality --zone quality.audit_trail --runs 20
```
Success: `pass`, and every row of its table shows the canary `none` and the positive control
`found`: the snapshot bucket (every golden snapshot decoded), Cloud Logging over the run
window, the dispatcher pod and its logs, every node's filesystem (scanned in place by a scan
pod), the laptop's `var/`. **The safety zone moves only after this passes.**

### 16. The leak check with the safety zone (35 min; USD 1.00)

```bash
deploy/gke/run.sh leakcheck --env gke --runs 24
```
Success: as step 15, with safety.sae_reconciler's runs included.

### 17. A full pass with measurements (15 min; USD 1.12)

```bash
deploy/gke/run.sh substrate bench --workers 12 --runs 200 --concurrency 12 --label gke-c12
deploy/gke/run.sh agents run --executor zoned --rerun --all-cuts    # the 910-run pass, 12 workers
deploy/gke/run.sh substrate bench --workers 2 --runs 1 --label gke-scale-back
```
Success: no failed run; a lost Spot worker shows as `RETRIED` and completes on another
worker. I record wall clock, p50 and p95 against kind (docs/gke-plan.md 2a). The pass's
reruns are recorded once (the GKE image digests are new provenance); a second identical
pass would record nothing.

### 18. Park, then the billable check (5 min; USD 1.16)

```bash
deploy/gke/park.sh
```
Success: the check exits 0 and lists the cluster with 0 nodes, no running instance, the
10 GiB Postgres disk unattached, the bucket and the repository, with the parked costs
(USD 0.10 an hour, the fee). The worker node's 10-minute scale-down delay may leave it
billing a little after step 17 (included above).

**The session: about 3 hours, about USD 1.16 including egress** (the cluster about 2.5
hours at 0.315: 0.79; one Spot worker about 2 hours at 0.078: 0.16, a second during the
pass: 0.02; egress about 2.7 GB, 1.7 GB of it past the free GiB: 0.20). A **guess** until
the billing report by SKU shows it the next day. Then USD 2.43 a day while parked, or
nothing after `deploy/gke/teardown.sh`.

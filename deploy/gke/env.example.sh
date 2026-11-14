# deploy/gke/env.example.sh: every variable docs/gke-session.md and deploy/gke/*.sh use.
# Copy to deploy/gke/env.sh (git-ignored), fill in the empty ones, and
# `source deploy/gke/env.sh` before each command block. teardown.sh, park.sh and
# resume.sh refuse to run while any variable exported here is unset or empty in your shell.
# No secrets belong here: every credential comes from gcloud's own login.

# The project and who pays.
export PROJECT_ID="meridian-fleet-agents"   # the one project this plan creates resources in
export PROJECT_NUMBER=""                    # gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)'; names the Workload Identity principals
export BILLING_ACCOUNT=""                   # gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)', without "billingAccounts/"; no free tier credit on it
export BUDGET_NAME="meridian-gke-2500inr"   # the budget's display name; teardown.sh deletes it unless KEEP_BUDGET=1
export BUDGET_AMOUNT="2500INR"              # gcloud billing budgets create --budget-amount; in the billing account's currency (INR); alerts at 50, 90 and 100 percent

# Where.
export REGION="us-central1"                 # Artifact Registry, the snapshot bucket, and the cluster's region
export ZONE="us-central1-a"                 # the zone of the nodes, their disks and the IAP bastion
export CLUSTER_LOCATION="${ZONE}"           # --location of every gcloud container command: the zone, as the cluster is zonal

# The cluster.
export CLUSTER_NAME="meridian-substrate"    # the GKE Standard cluster
export CLUSTER_VERSION="1.37.1-gke.1552000" # pinned; step 1 of the runbook checks Regular still carries it
export RELEASE_CHANNEL="regular"            # the channel that carries CLUSTER_VERSION
export SYSTEM_POOL="default-pool"           # the pool `clusters create` makes: ate-system, Postgres and the dispatcher run here
export SYSTEM_MACHINE_TYPE="c3-standard-4"  # Google's minimum for setup-gcp; Postgres requests 2 vCPU
export WORKER_POOL="substrate"              # the Spot pool for Substrate's workers, tainted ate.dev/sandboxClass=gvisor:NoSchedule
export WORKER_MACHINE_TYPE="c3-standard-4"  # Spot; 12 workers fit on one node
export WORKER_MAX_NODES="2"                 # the Spot pool autoscales from 0 to this

# Identities, storage and images.
export NODE_GSA_NAME="ate-gke-node"                        # the nodes' service account (setup-gcp's default name)
export SNAPSHOT_TRANSFER_GSA_NAME="ate-snapshot-transfer"  # setup-gcp's service account for snapshot transfers
export BUCKET_NAME="${PROJECT_ID}-ate-snapshots"           # Substrate's snapshot bucket, at most 56 characters; setup-gcp also makes ${BUCKET_NAME}-assets
export AR_REPO="meridian"                                  # the Artifact Registry Docker repository in $REGION for meridian-agent and meridian-dispatcher
export ATESPACE="meridian"                                 # the Substrate atespace Meridian's actors run in
export BASTION_NAME="meridian-iap-bastion"                 # the IAP fallback VM (docs/gke-plan.md 6.11), only if the DNS endpoint fails; park.sh stops it, teardown.sh deletes it

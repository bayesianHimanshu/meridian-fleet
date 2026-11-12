#!/usr/bin/env bash
# Deletes everything docs/gke-plan.md creates, then checks that nothing billable remains.
#
#   source deploy/gke/env.sh
#   deploy/gke/teardown.sh           # delete, then check (asks before deleting)
#   deploy/gke/teardown.sh --check   # only the check: list what is still there, change nothing
#
# Order: the Kubernetes volumes first (a PersistentVolume's disk is deleted with its
# claim, not with the cluster), then the cluster (its node pools, boot disks, firewall
# rules and routes go with it), any leftover dynamically provisioned disk, the snapshot
# and asset buckets with their contents, the Artifact Registry repository, the service
# accounts and the project binding, and last the budget (KEEP_BUDGET=1 keeps it).
# Bucket and repository IAM bindings go with their resources. Enabled APIs are left on:
# they cost nothing (DISABLE_APIS=1 turns off the ones the plan enabled).
#
# A resource that is already gone counts as deleted, so a rerun after a partial teardown
# is safe. A failure does not stop the run; the script exits 1 with one FAILED line per
# failed step, and always exits 1 when the check finds anything billable left.
set -o errexit -o nounset -o pipefail

check_only=0
case "${1:-}" in
  --check) check_only=1 ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

for var in PROJECT_ID PROJECT_NUMBER REGION ZONE CLUSTER_NAME BUCKET_NAME AR_REPO \
  NODE_GSA_NAME SNAPSHOT_TRANSFER_GSA_NAME BILLING_ACCOUNT BUDGET_NAME; do
  if [ -z "${!var:-}" ]; then
    echo "${var} is not set: source deploy/gke/env.sh first" >&2
    exit 2
  fi
done
gc=(gcloud --project="${PROJECT_ID}" --quiet)
failed=()

gone() { grep -qiE 'not found|NOT_FOUND|code=404|404|matched no objects|does not exist' "$1"; }

# step NAME CMD... runs CMD; "not found" counts as done.
step() {
  local name="$1" log
  shift
  log="$(mktemp)"
  echo "== ${name}"
  if "$@" >"${log}" 2>&1; then
    cat "${log}"
  elif gone "${log}"; then
    echo "   already gone"
  else
    cat "${log}"
    failed+=("${name}")
  fi
  rm -f "${log}"
}

delete_volumes() {
  # Only when the cluster still exists. Deleting the claims lets the PD CSI driver delete
  # their disks (reclaim policy Delete); the cluster's deletion would leave them behind.
  if ! "${gc[@]}" container clusters describe "${CLUSTER_NAME}" --location="${ZONE}" \
    --format='value(name)' >/dev/null 2>&1; then
    echo "   no cluster, no claims to delete"
    return 0
  fi
  "${gc[@]}" container clusters get-credentials "${CLUSTER_NAME}" --location="${ZONE}" \
    --dns-endpoint >/dev/null
  kubectl delete namespace meridian --ignore-not-found --wait=true --timeout=300s
  kubectl delete pvc --all -n ate-system --ignore-not-found --wait=true --timeout=300s
  # Wait for the disks behind the released volumes to be deleted.
  for _ in $(seq 1 30); do
    [ -z "$(kubectl get pv -o name 2>/dev/null)" ] && return 0
    sleep 10
  done
  echo "PersistentVolumes still present after 300 s:" >&2
  kubectl get pv >&2
  return 1
}

leftover_disks() {
  # Disks GKE's PD CSI driver provisioned for this cluster's claims, unattached. GKE names
  # them pvc-<uid> and records the claim in the description (guess: checked by the
  # listing in check(), which shows every disk in the project whatever this matches).
  "${gc[@]}" compute disks list \
    --filter="zone:${ZONE} AND name~^pvc- AND -users:* AND description~kubernetes.io/created-for" \
    --format='value(name)'
}

delete_leftover_disks() {
  local d
  for d in $(leftover_disks); do
    "${gc[@]}" compute disks delete "${d}" --zone="${ZONE}"
  done
}

delete_bucket() {
  # Objects and the bucket in one command.
  if "${gc[@]}" storage buckets describe "gs://$1" --format='value(name)' >/dev/null 2>&1; then
    "${gc[@]}" storage rm --recursive "gs://$1"
  else
    echo "   gs://$1 not found"
  fi
}

delete_sa() {
  local email="$1@${PROJECT_ID}.iam.gserviceaccount.com"
  if "${gc[@]}" iam service-accounts list --format='value(email)' | grep -qx "${email}"; then
    "${gc[@]}" iam service-accounts delete "${email}"
  else
    echo "   ${email} not found"
  fi
}

delete_budget() {
  local id
  id="$(gcloud billing budgets list --billing-account="${BILLING_ACCOUNT}" \
    --filter="displayName=${BUDGET_NAME}" --format='value(name)')"
  if [ -z "${id}" ]; then
    echo "   budget ${BUDGET_NAME} not found"
    return 0
  fi
  gcloud billing budgets delete "${id}" --quiet
}

# check: everything that can bill in this project, listed; exit 1 if anything is there.
check() {
  local left=0 out
  echo
  echo "== check: billable resources left in ${PROJECT_ID}"
  while IFS='|' read -r label cmd; do
    if ! out="$(eval "${cmd}" 2>&1)"; then
      echo "   ${label}: could not list (${out})"
      left=1
    elif [ -n "${out}" ]; then
      echo "   ${label}: STILL PRESENT"
      printf '%s\n' "${out}" | sed 's/^/     /'
      left=1
    else
      echo "   ${label}: none"
    fi
  done <<EOF
GKE clusters|"\${gc[@]}" container clusters list --format='value(name,location)'
VM instances (nodes)|"\${gc[@]}" compute instances list --format='value(name,zone)'
Persistent disks|"\${gc[@]}" compute disks list --format='value(name,zone,sizeGb)'
Disk snapshots|"\${gc[@]}" compute snapshots list --format='value(name)'
Custom images|"\${gc[@]}" compute images list --no-standard-images --format='value(name)'
Static IP addresses|"\${gc[@]}" compute addresses list --format='value(name,region)'
Forwarding rules (load balancers)|"\${gc[@]}" compute forwarding-rules list --format='value(name,region)'
Cloud Routers (NAT)|"\${gc[@]}" compute routers list --format='value(name,region)'
Cloud Storage buckets|"\${gc[@]}" storage buckets list --format='value(name)'
Artifact Registry repositories|"\${gc[@]}" artifacts repositories list --location="\${REGION}" --format='value(name)'
Cloud SQL instances|"\${gc[@]}" sql instances list --format='value(name)' 2>/dev/null || true
Filestore instances|"\${gc[@]}" filestore instances list --format='value(name)' 2>/dev/null || true
EOF
  echo "   (not billable, shown for completeness)"
  echo "   service accounts: $("${gc[@]}" iam service-accounts list --format='value(email)' | tr '\n' ' ')"
  echo "   budgets: $(gcloud billing budgets list --billing-account="${BILLING_ACCOUNT}" \
    --format='value(displayName)' 2>/dev/null | tr '\n' ' ')"
  echo "   Billing reports lag by up to a day: confirm a zero daily cost in the console"
  echo "   (Billing > Reports, group by SKU) the day after."
  return "${left}"
}

if [ "${check_only}" -eq 1 ]; then
  check
  exit $?
fi

echo "This deletes cluster ${CLUSTER_NAME} (${ZONE}), gs://${BUCKET_NAME} and"
echo "gs://${BUCKET_NAME}-assets with their contents, repository ${AR_REPO} (${REGION}),"
echo "service accounts ${NODE_GSA_NAME} and ${SNAPSHOT_TRANSFER_GSA_NAME}, and budget"
echo "${BUDGET_NAME}, in project ${PROJECT_ID}. Golden snapshots and images are lost."
read -r -p "Type the project id to continue: " answer
[ "${answer}" = "${PROJECT_ID}" ] || { echo "not confirmed; nothing deleted" >&2; exit 2; }

step "Kubernetes volumes (Postgres claim)" delete_volumes
step "cluster ${CLUSTER_NAME}" "${gc[@]}" container clusters delete "${CLUSTER_NAME}" --location="${ZONE}"
step "leftover volume disks" delete_leftover_disks
step "bucket ${BUCKET_NAME}" delete_bucket "${BUCKET_NAME}"
step "bucket ${BUCKET_NAME}-assets" delete_bucket "${BUCKET_NAME}-assets"
step "Artifact Registry ${AR_REPO}" "${gc[@]}" artifacts repositories delete "${AR_REPO}" --location="${REGION}"
step "project binding of ${NODE_GSA_NAME}" "${gc[@]}" projects remove-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${NODE_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/container.defaultNodeServiceAccount --condition=None
step "service account ${SNAPSHOT_TRANSFER_GSA_NAME}" delete_sa "${SNAPSHOT_TRANSFER_GSA_NAME}"
step "service account ${NODE_GSA_NAME}" delete_sa "${NODE_GSA_NAME}"
if [ "${KEEP_BUDGET:-0}" != "1" ]; then
  step "budget ${BUDGET_NAME}" delete_budget
fi
if [ "${DISABLE_APIS:-0}" = "1" ]; then
  # The APIs setup-gcp enables beyond those a new project has; container first.
  step "APIs" "${gc[@]}" services disable container.googleapis.com artifactregistry.googleapis.com \
    networkconnectivity.googleapis.com cloudtrace.googleapis.com telemetry.googleapis.com
fi

status=0
check || status=1
for f in "${failed[@]}"; do
  echo "FAILED: ${f}" >&2
  status=1
done
exit "${status}"

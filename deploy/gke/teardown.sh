#!/usr/bin/env bash
# Deletes everything docs/gke-plan.md creates, then checks that nothing billable remains.
#
#   source deploy/gke/env.sh
#   deploy/gke/teardown.sh           # delete, then check (asks before deleting)
#   deploy/gke/teardown.sh --check   # only the check: list what is still there, change nothing
#
# For the end of phase 4b, or a cluster that would sit idle for more than a week; between
# sessions use deploy/gke/park.sh.
#
# Order: the Kubernetes volumes first (a PersistentVolume's disk is deleted with its
# claim, not with the cluster), then the cluster (its node pools, boot disks, firewall
# rules and routes go with it), any leftover dynamically provisioned disk, the IAP
# bastion and its firewall rule (if the fallback was used), the snapshot and asset
# buckets with their contents, the Artifact Registry repository, the service accounts and
# the project binding, and last the budget (KEEP_BUDGET=1 keeps it). Bucket and repository
# IAM bindings go with their resources. Enabled APIs are left on: they cost nothing
# (DISABLE_APIS=1 turns off the ones the plan enabled).
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
# shellcheck source=deploy/gke/lib.sh
source "$(dirname "$0")/lib.sh"

delete_volumes() {
  if ! cluster_exists; then
    echo "   no cluster, no claims to delete"
    return 0
  fi
  credentials
  kubectl delete namespace meridian meridian-dispatch --ignore-not-found --wait=true --timeout=300s
  kubectl delete pvc --all -n ate-system --ignore-not-found --wait=true --timeout=300s
  for _ in $(seq 1 30); do
    [ -z "$(kubectl get pv -o name 2>/dev/null)" ] && return 0
    sleep 10
  done
  echo "PersistentVolumes still present after 300 s:" >&2
  kubectl get pv >&2
  return 1
}

leftover_disks() {
  # Disks the PD CSI driver made for this cluster's claims, unattached (guess at the
  # filter; the check lists every disk in the project whatever this matches).
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

delete_bastion() {
  if bastion_exists; then
    "${gc[@]}" compute instances delete "${BASTION_NAME}" --zone="${ZONE}" --delete-disks=all
  else
    echo "   no bastion"
  fi
  if "${gc[@]}" compute firewall-rules describe allow-iap-ssh-bastion --format='value(name)' >/dev/null 2>&1; then
    "${gc[@]}" compute firewall-rules delete allow-iap-ssh-bastion
  fi
}

delete_bucket() {
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

if [ "${check_only}" -eq 1 ]; then
  list_billable gone
  exit $?
fi

echo "This deletes cluster ${CLUSTER_NAME} (${CLUSTER_LOCATION}), bastion ${BASTION_NAME} if any,"
echo "gs://${BUCKET_NAME} and gs://${BUCKET_NAME}-assets with their contents, repository"
echo "${AR_REPO} (${REGION}), service accounts ${NODE_GSA_NAME} and"
echo "${SNAPSHOT_TRANSFER_GSA_NAME}, and budget ${BUDGET_NAME}, in project ${PROJECT_ID}."
echo "Golden snapshots, Substrate's Postgres and the images are lost."
read -r -p "Type the project id to continue: " answer
[ "${answer}" = "${PROJECT_ID}" ] || { echo "not confirmed; nothing deleted" >&2; exit 2; }

step "Kubernetes volumes (Postgres claim)" delete_volumes
step "cluster ${CLUSTER_NAME}" "${gc[@]}" container clusters delete "${CLUSTER_NAME}" --location="${CLUSTER_LOCATION}"
step "leftover volume disks" delete_leftover_disks
step "bastion ${BASTION_NAME} and its IAP firewall rule" delete_bastion
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
  step "APIs" "${gc[@]}" services disable container.googleapis.com artifactregistry.googleapis.com \
    networkconnectivity.googleapis.com cloudtrace.googleapis.com telemetry.googleapis.com
fi

status=0
list_billable gone || status=1
finish "${status}"

#!/usr/bin/env bash
# Parks the GKE environment between sessions: no node runs, everything else stays.
#
#   source deploy/gke/env.sh
#   deploy/gke/park.sh            # park, then check what still bills
#   deploy/gke/park.sh --check    # only the check
#
# Order: the WorkerPool to 0 workers (so nothing asks the autoscaler for a worker node),
# wait until no worker pod is left, the worker pool to 0 nodes, then the system pool to 0
# nodes, and the IAP bastion stopped if it exists. Kept: the cluster and its control plane
# (GKE runs it; one zonal cluster's fee is covered by the free tier credit), the Postgres
# PersistentVolume (a zonal disk, detached; Substrate's actors, templates and tags), the
# snapshot bucket (golden snapshots), the repository (images). Before parking it records
# the Postgres claim's uid and the golden tags in var/gke/parked.json so resume.sh can show
# they came back unchanged.
#
# Park while phase 4b is active; teardown.sh at its end or before an idle week.
set -o errexit -o nounset -o pipefail

check_only=0
case "${1:-}" in
  --check) check_only=1 ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac
# shellcheck source=deploy/gke/lib.sh
source "$(dirname "$0")/lib.sh"
STATE="var/gke/parked.json"

if [ "${check_only}" -eq 1 ]; then
  list_billable parked
  exit $?
fi
cluster_exists || { echo "no cluster ${CLUSTER_NAME} in ${ZONE}: nothing to park" >&2; exit 1; }
credentials

record_state() {
  mkdir -p "$(dirname "${STATE}")"
  local pvc tags
  pvc="$(kubectl -n ate-system get pvc data-postgres-0 -o jsonpath='{.metadata.uid} {.spec.volumeName} {.status.capacity.storage}')"
  tags="$(kubectl ate get tag -a "${ATESPACE:-meridian}" -o json | python3 -c 'import json,sys; print(" ".join(sorted(t["metadata"]["uid"] for t in json.load(sys.stdin).get("tags", []))))')"
  printf '{"pvc": "%s", "tags": "%s", "parked_at": "%s"}\n' "${pvc}" "${tags}" "$(date -u +%FT%TZ)" > "${STATE}"
  echo "   recorded ${STATE}: claim ${pvc}; $(wc -w <<<"${tags}") golden tags"
}

workers_to_zero() {
  kubectl -n meridian scale workerpool meridian --replicas=0
  for _ in $(seq 1 60); do
    [ -z "$(kubectl -n meridian get pods -l ate.dev/worker-pool=meridian -o name 2>/dev/null)" ] && return 0
    sleep 5
  done
  echo "worker pods still present after 300 s" >&2
  return 1
}

step "record the Postgres claim and the golden tags" record_state
step "WorkerPool to 0 workers" workers_to_zero
step "pool ${WORKER_POOL} to 0 nodes" "${gc[@]}" container clusters resize "${CLUSTER_NAME}" \
  --location="${ZONE}" --node-pool="${WORKER_POOL}" --num-nodes=0
step "pool ${SYSTEM_POOL} to 0 nodes" "${gc[@]}" container clusters resize "${CLUSTER_NAME}" \
  --location="${ZONE}" --node-pool="${SYSTEM_POOL}" --num-nodes=0
if bastion_exists; then
  step "stop bastion ${BASTION_NAME}" "${gc[@]}" compute instances stop "${BASTION_NAME}" --zone="${ZONE}"
fi

status=0
list_billable parked || status=1
finish "${status}"

#!/usr/bin/env bash
# Parks the GKE environment between sessions: no node runs, everything else stays.
#
#   source deploy/gke/env.sh
#   deploy/gke/park.sh            # park, then check what still bills
#   deploy/gke/park.sh --check    # only the check
#
# Order: record the Postgres claim and the golden tags; stop every workload with
# deploy/gke/workloads.sh (the WorkerPool, the dispatcher, ate-system's Deployments, then
# Postgres, each scaled to 0 with its count in an annotation; no budget bypassed, nothing
# left for a drain to wait on); then both node pools to 0 nodes and the IAP bastion
# stopped if it exists. A resize gcloud stops waiting for while GKE's operation still runs
# is reported IN PROGRESS (exit 3) with the operation to check, not FAILED. Kept: the cluster and its control plane
# (GKE runs it for the management fee, USD 0.10 an hour, about 2.40 a day: this billing
# account has no free tier credit, so a parked day costs about what 8 hours of a session's
# system node do; teardown.sh bills nothing), the Postgres
# PersistentVolume (a zonal disk, detached; Substrate's actors, templates and tags), the
# snapshot bucket (golden snapshots), the repository (images). Before parking it records
# the Postgres claim's uid and the golden tags in var/gke/parked.json so resume.sh can show
# they came back unchanged.
#
# Decided: park while phase 4b is active; teardown.sh at its end or before an idle week.
# Without the free tier, one parked day (about USD 2.43) costs more in money than tearing
# down and recreating (about USD 0.40 of cluster time); what parking saves is about an hour
# of your setup (docs/gke-session.md, "Park or tear down").
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
cluster_exists || { echo "no cluster ${CLUSTER_NAME} in ${CLUSTER_LOCATION}: nothing to park" >&2; exit 1; }
credentials

record_state() {
  mkdir -p "$(dirname "${STATE}")"
  local pvc tags
  pvc="$(kubectl -n ate-system get pvc data-postgres-0 -o jsonpath='{.metadata.uid} {.spec.volumeName} {.status.capacity.storage}')"
  tags="$(kubectl ate get tag -a "${ATESPACE:-meridian}" -o json | python3 -c 'import json,sys; print(" ".join(sorted(t["metadata"]["uid"] for t in json.load(sys.stdin).get("tags", []))))')"
  printf '{"pvc": "%s", "tags": "%s", "parked_at": "%s"}\n' "${pvc}" "${tags}" "$(date -u +%FT%TZ)" > "${STATE}"
  echo "   recorded ${STATE}: claim ${pvc}; $(wc -w <<<"${tags}") golden tags"
}

step "record the Postgres claim and the golden tags" record_state
step "stop the workloads (WorkerPool, dispatcher, ate-system, Postgres)" \
  "$(dirname "$0")/workloads.sh" --context "$(kubectl config current-context)" stop
if [ "${#failed[@]}" -gt 0 ]; then
  echo "workloads still running: pools not resized (the drain would wait on them)" >&2
  finish 1
fi
resize_pool "${WORKER_POOL}" 0
resize_pool "${SYSTEM_POOL}" 0
if bastion_exists; then
  step "stop bastion ${BASTION_NAME}" "${gc[@]}" compute instances stop "${BASTION_NAME}" --zone="${ZONE}"
fi

status=0
list_billable parked || status=1
finish "${status}"

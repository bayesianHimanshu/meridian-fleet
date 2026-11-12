#!/usr/bin/env bash
# Resumes a parked GKE environment: the system node back, Substrate checked, workers on ask.
#
#   source deploy/gke/env.sh
#   deploy/gke/resume.sh               # system pool to 1 node; WorkerPool stays at 0
#   WORKERS=12 deploy/gke/resume.sh    # also scale the WorkerPool for a pass
#   deploy/gke/resume.sh --check       # only the check of what bills
#
# Order: the system pool to 1 node, wait for every ate-system pod and the dispatcher to be
# Ready, then compare the Postgres claim (uid, volume, size) and the golden tags with what
# park.sh recorded: they must be the same, or the script fails (Substrate's state did not
# survive). Then start the bastion if it exists, scale the WorkerPool if WORKERS is set
# (the autoscaler adds worker nodes for the pending workers), and list what bills.
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
cluster_exists || { echo "no cluster ${CLUSTER_NAME} in ${ZONE}" >&2; exit 1; }

wait_ready() {
  credentials
  kubectl wait --for=condition=Ready nodes --all --timeout=600s
  kubectl -n ate-system rollout status statefulset/postgres --timeout=600s
  for d in $(kubectl -n ate-system get deployments -o name); do
    kubectl -n ate-system rollout status "${d}" --timeout=600s
  done
  kubectl -n meridian-dispatch rollout status deployment/meridian-dispatcher --timeout=300s
}

compare_state() {
  [ -f "${STATE}" ] || { echo "no ${STATE}: was this cluster parked with park.sh?" >&2; return 1; }
  local pvc tags
  pvc="$(kubectl -n ate-system get pvc data-postgres-0 -o jsonpath='{.metadata.uid} {.spec.volumeName} {.status.capacity.storage}')"
  tags="$(kubectl ate get tag -a "${ATESPACE:-meridian}" -o json | python3 -c 'import json,sys; print(" ".join(sorted(t["metadata"]["uid"] for t in json.load(sys.stdin).get("tags", []))))')"
  python3 - "${STATE}" "${pvc}" "${tags}" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
pvc, tags = sys.argv[2], sys.argv[3]
ok = state["pvc"] == pvc and state["tags"] == tags
print(f"   Postgres claim: {'same' if state['pvc'] == pvc else 'CHANGED'} ({pvc})")
print(f"   golden tags: {'same' if state['tags'] == tags else 'CHANGED'} ({len(tags.split())})")
sys.exit(0 if ok else 1)
PY
}

step "pool ${SYSTEM_POOL} to 1 node" "${gc[@]}" container clusters resize "${CLUSTER_NAME}" \
  --location="${ZONE}" --node-pool="${SYSTEM_POOL}" --num-nodes=1
step "ate-system, Postgres and the dispatcher Ready" wait_ready
step "Substrate's state survived the park" compare_state
if bastion_exists; then
  step "start bastion ${BASTION_NAME}" "${gc[@]}" compute instances start "${BASTION_NAME}" --zone="${ZONE}"
fi
if [ -n "${WORKERS:-}" ]; then
  step "WorkerPool to ${WORKERS} workers" kubectl -n meridian scale workerpool meridian --replicas="${WORKERS}"
fi

status=0
list_billable parked || status=1
finish "${status}"

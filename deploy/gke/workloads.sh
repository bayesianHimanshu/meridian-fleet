#!/usr/bin/env bash
# deploy/gke/workloads.sh: stop or start every workload Substrate and Meridian run, with
# kubectl only, on any cluster (kind exercises it: tests/substrate/test_park.py).
#
#   deploy/gke/workloads.sh --context CTX stop     # before parking (park.sh)
#   deploy/gke/workloads.sh --context CTX start    # after the system node is back (resume.sh)
#   deploy/gke/workloads.sh --context CTX status
#
# Why: a node pool resized to 0 drains its nodes, and the drain honours PodDisruptionBudgets.
# ate-api-server and ate-idp-server have maxUnavailable 1, so with every node going their
# last pods can go nowhere and GKE waits up to an hour (the first session, docs/gke-plan.md
# section 9). Scaling the workloads to 0 first leaves the drain nothing to wait for, without
# bypassing any budget: a scale-down is not an eviction.
#
# stop, in order: the WorkerPools (the controller must still run to remove their workers),
# the dispatcher, every ate-system Deployment, then the StatefulSets (Postgres); each gets
# the annotation meridian.dev/parked-replicas with its replica count before it goes to 0.
# Then it waits until no pod but a DaemonSet's is left in those namespaces. It refuses while
# any actor exists (a pass is running). start reverses the order, restores each recorded
# count, waits for each rollout, and removes the annotations. Neither touches a
# PersistentVolumeClaim, a template, a tag or a DaemonSet.
set -o errexit -o nounset -o pipefail

ANNOTATION="meridian.dev/parked-replicas"
SYSTEM_NS="ate-system"
DISPATCH_NS="meridian-dispatch"
POOL_NS="meridian"
ATESPACE="${ATESPACE:-meridian}"
WAIT_SECONDS="${WAIT_SECONDS:-600}"

context=""
while [ $# -gt 1 ]; do
  case "$1" in
    --context) context="$2"; shift 2 ;;
    *) break ;;
  esac
done
action="${1:-}"
if [ -z "${context}" ] || [ -z "${action}" ]; then
  echo "usage: $0 --context CONTEXT stop|start|status" >&2
  exit 2
fi
k=(kubectl --context "${context}")

# kind/name of every object stop and start manage, in stop order.
objects() {
  local ns kind
  for kind in workerpool; do
    "${k[@]}" -n "${POOL_NS}" get "${kind}" -o name 2>/dev/null | sed "s|^|${POOL_NS} |"
  done
  "${k[@]}" -n "${DISPATCH_NS}" get deployment -o name 2>/dev/null | sed "s|^|${DISPATCH_NS} |"
  for ns in "${SYSTEM_NS}"; do
    "${k[@]}" -n "${ns}" get deployment -o name | sed "s|^|${ns} |"
    "${k[@]}" -n "${ns}" get statefulset -o name | sed "s|^|${ns} |"
  done
}

replicas() { "${k[@]}" -n "$1" get "$2" -o jsonpath='{.spec.replicas}'; }
recorded() { "${k[@]}" -n "$1" get "$2" -o jsonpath="{.metadata.annotations.meridian\.dev/parked-replicas}"; }

# Running or pending pods that are not a DaemonSet's, in the managed namespaces (a
# finished Job's pod holds nothing and a drain does not wait for it).
leftover_pods() {
  local ns
  for ns in "${POOL_NS}" "${DISPATCH_NS}" "${SYSTEM_NS}"; do
    "${k[@]}" -n "${ns}" get pods -o json 2>/dev/null | python3 -c '
import json, sys
ns = sys.argv[1]
for p in json.load(sys.stdin).get("items", []):
    owners = [o.get("kind") for o in p["metadata"].get("ownerReferences", [])]
    finished = p.get("status", {}).get("phase") in ("Succeeded", "Failed")  # a done Job
    if "DaemonSet" not in owners and not finished:
        print(ns + "/" + p["metadata"]["name"])
' "${ns}"
  done
}

live_actors() {
  kubectl ate --context "${context}" get actors -a "${ATESPACE}" -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("actors", [])))'
}

stop() {
  local n
  n="$(live_actors || echo unknown)"
  if [ "${n}" != "0" ]; then
    echo "refused: ${n} actor(s) exist in ${ATESPACE} (a pass is running?); stop nothing" >&2
    return 1
  fi
  while read -r ns obj; do
    [ -n "${obj}" ] || continue
    if [ -z "$(recorded "${ns}" "${obj}")" ]; then
      "${k[@]}" -n "${ns}" annotate "${obj}" "${ANNOTATION}=$(replicas "${ns}" "${obj}")" >/dev/null
    fi
    "${k[@]}" -n "${ns}" scale "${obj}" --replicas=0 >/dev/null
    echo "   ${ns}/${obj#*/}: 0 (was $(recorded "${ns}" "${obj}"))"
    if [ "${obj%%/*}" = "workerpool.ate.dev" ]; then
      wait_gone "${ns}" "ate.dev/worker-pool=${obj#*/}"
    fi
  done < <(objects)
  local deadline=$((SECONDS + WAIT_SECONDS)) left
  while left="$(leftover_pods)" && [ -n "${left}" ]; do
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "pods still present after ${WAIT_SECONDS} s:" >&2
      echo "${left}" >&2
      return 1
    fi
    sleep 3
  done
  echo "   no pod left but DaemonSets'"
}

wait_gone() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  while [ -n "$("${k[@]}" -n "$1" get pods -l "$2" -o name 2>/dev/null)" ]; do
    [ "${SECONDS}" -lt "${deadline}" ] || { echo "pods $2 still present" >&2; return 1; }
    sleep 3
  done
}

start() {
  local ns obj n
  # Reverse order: Postgres first, then ate-system, the dispatcher, the WorkerPools.
  while read -r ns obj; do
    [ -n "${obj}" ] || continue
    n="$(recorded "${ns}" "${obj}")"
    if [ -z "${n}" ]; then
      echo "   ${ns}/${obj#*/}: not parked, left as it is"
      continue
    fi
    if [ "${obj%%/*}" = "workerpool.ate.dev" ] && [ -n "${WORKERS:-}" ]; then
      n="${WORKERS}"
    fi
    "${k[@]}" -n "${ns}" scale "${obj}" --replicas="${n}" >/dev/null
    case "${obj%%/*}" in
      deployment.apps|statefulset.apps)
        "${k[@]}" -n "${ns}" rollout status "${obj}" --timeout="${WAIT_SECONDS}s" >/dev/null ;;
      workerpool.ate.dev)
        "${k[@]}" -n "${ns}" wait "${obj}" --for=jsonpath='{.status.readyReplicas}'="${n}" \
          --timeout="${WAIT_SECONDS}s" >/dev/null ;;
    esac
    "${k[@]}" -n "${ns}" annotate "${obj}" "${ANNOTATION}-" >/dev/null
    echo "   ${ns}/${obj#*/}: ${n}, ready"
  done < <(objects | tac)
}

status() {
  local ns obj parked=0 total=0
  while read -r ns obj; do
    [ -n "${obj}" ] || continue
    total=$((total + 1))
    local r a
    r="$(replicas "${ns}" "${obj}")"
    a="$(recorded "${ns}" "${obj}")"
    [ -n "${a}" ] && [ "${r}" = "0" ] && parked=$((parked + 1))
    echo "   ${ns}/${obj#*/}: replicas ${r}${a:+, parked from ${a}}"
  done < <(objects)
  echo "   ${parked} of ${total} parked"
}

case "${action}" in
  stop) stop ;;
  start) start ;;
  status) status ;;
  *) echo "usage: $0 --context CONTEXT stop|start|status" >&2; exit 2 ;;
esac

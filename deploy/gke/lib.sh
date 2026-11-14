# Shared by deploy/gke/teardown.sh, park.sh and resume.sh (sourced, not run).
# Requires every variable deploy/gke/env.example.sh exports, set and not empty (copy it to
# deploy/gke/env.sh, fill it in, source it); changes nothing by itself.

_example="$(dirname "${BASH_SOURCE[0]}")/env.example.sh"
_missing=()
while IFS= read -r var; do
  [ -n "${!var:-}" ] || _missing+=("${var}")
done < <(sed -n 's/^export \([A-Z_][A-Z0-9_]*\)=.*/\1/p' "${_example}")
if [ "${#_missing[@]}" -gt 0 ]; then
  echo "not set: ${_missing[*]}" >&2
  echo "every variable of deploy/gke/env.example.sh must be set: update deploy/gke/env.sh and source it" >&2
  exit 2
fi
unset _example _missing
gc=(gcloud --project="${PROJECT_ID}" --quiet)
failed=()

gone() { grep -qiE 'not found|NOT_FOUND|code=404|404|matched no objects|does not exist' "$1"; }

# step NAME CMD... runs CMD; "not found" counts as done; a failure is recorded, not fatal.
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

cluster_exists() {
  "${gc[@]}" container clusters describe "${CLUSTER_NAME}" --location="${CLUSTER_LOCATION}" \
    --format='value(name)' >/dev/null 2>&1
}

credentials() {
  "${gc[@]}" container clusters get-credentials "${CLUSTER_NAME}" --location="${CLUSTER_LOCATION}" \
    --dns-endpoint >/dev/null
}

bastion_exists() {
  "${gc[@]}" compute instances describe "${BASTION_NAME}" --zone="${ZONE}" \
    --format='value(name)' >/dev/null 2>&1
}

# list_billable: every resource that can bill in the project, one block each; returns 1
# if anything is listed. MODE=parked tolerates what a parked cluster keeps (the cluster,
# its unattached Postgres disk, a stopped bastion's disk, the buckets and the repository)
# and lists it with what it costs; MODE=gone tolerates nothing.
list_billable() {
  local mode="$1" left=0 out label cmd
  echo
  echo "== check (${mode}): billable resources in ${PROJECT_ID}"
  while IFS='|' read -r label cmd; do
    if ! out="$(eval "${cmd}" 2>&1)"; then
      echo "   ${label}: could not list (${out})"
      left=1
    elif [ -n "${out}" ]; then
      echo "   ${label}:"
      printf '%s\n' "${out}" | sed 's/^/     /'
      case "${mode}:${label}" in
        parked:"GKE clusters"|parked:"Persistent disks"|parked:"Cloud Storage buckets"|parked:"Artifact Registry repositories") ;;
        parked:"VM instances (nodes, bastion)")
          # A parked project may keep the bastion, stopped (TERMINATED): disk only.
          if printf '%s\n' "${out}" | grep -qv 'TERMINATED'; then left=1; fi ;;
        *) left=1 ;;
      esac
    else
      echo "   ${label}: none"
    fi
  done <<EOF
GKE clusters|"\${gc[@]}" container clusters list --format='value(name,location,currentNodeCount)'
VM instances (nodes, bastion)|"\${gc[@]}" compute instances list --format='value(name,zone,status)'
Persistent disks|"\${gc[@]}" compute disks list --format='value(name,zone,sizeGb,type.basename(),users.len())'
Disk snapshots|"\${gc[@]}" compute snapshots list --format='value(name)'
Custom images|"\${gc[@]}" compute images list --no-standard-images --format='value(name)'
Static IP addresses|"\${gc[@]}" compute addresses list --format='value(name,region)'
Forwarding rules (load balancers)|"\${gc[@]}" compute forwarding-rules list --format='value(name,region)'
Cloud Routers (NAT)|"\${gc[@]}" compute routers list --format='value(name,region)'
Cloud Storage buckets|"\${gc[@]}" storage buckets list --format='value(name)'
Artifact Registry repositories|"\${gc[@]}" artifacts repositories list --location="\${REGION}" --format='value(name)' 2>&1 | sed -e '/^Listing items under/d' -e '/^[[:space:]]*$/d'
Cloud SQL instances|"\${gc[@]}" sql instances list --format='value(name)' 2>/dev/null || true
Filestore instances|"\${gc[@]}" filestore instances list --format='value(name)' 2>/dev/null || true
EOF
  if [ "${mode}" = "parked" ]; then
    echo "   parked costs (docs/gke-plan.md section 5): the cluster management fee USD 0.10/h"
    echo "   (about USD 2.40 a day: this billing account has no free tier credit), billed while"
    echo "   the cluster exists even with no node; the 10 GiB Postgres disk about USD 1 a month;"
    echo "   a stopped bastion's 10 GB disk about USD 0.40 a month; the bucket and the repository"
    echo "   within their free tiers. No node runs. Teardown (teardown.sh) bills nothing."
  fi
  echo "   (not billable) service accounts: $("${gc[@]}" iam service-accounts list --format='value(email)' | tr '\n' ' ')"
  echo "   Billing reports lag by up to a day: check Billing > Reports by SKU the next day."
  return "${left}"
}

# resize_pool POOL N: resize a node pool. If gcloud stops waiting while GKE's operation
# still runs (a drain can take long), that is "in progress", not a failure: the operation
# is recorded and finish() exits 3 with how to check it later.
in_progress=()
resize_pool() {
  local pool="$1" nodes="$2" op
  echo "== pool ${pool} to ${nodes} node(s)"
  if "${gc[@]}" container clusters resize "${CLUSTER_NAME}" --location="${CLUSTER_LOCATION}" \
    --node-pool="${pool}" --num-nodes="${nodes}" >/dev/null 2>"${TMPDIR:-/tmp}/resize.$$"; then
    echo "   done"
  else
    op="$("${gc[@]}" container operations list --location="${CLUSTER_LOCATION}" \
      --filter="status=RUNNING AND operationType=SET_NODE_POOL_SIZE AND targetLink~nodePools/${pool}\$" \
      --format='value(name)' | head -1)"
    if [ -n "${op}" ]; then
      echo "   in progress: operation ${op} still running"
      in_progress+=("pool ${pool} to ${nodes}: operation ${op}")
    else
      cat "${TMPDIR:-/tmp}/resize.$$" >&2
      failed+=("pool ${pool} to ${nodes} node(s)")
    fi
  fi
  rm -f "${TMPDIR:-/tmp}/resize.$$"
}

finish() {
  local status="$1" f
  for f in "${failed[@]}"; do
    echo "FAILED: ${f}" >&2
    status=1
  done
  if [ "${status}" -eq 0 ] || [ "${#failed[@]}" -eq 0 ]; then
    for f in "${in_progress[@]}"; do
      echo "IN PROGRESS: ${f}"
      echo "   gcloud container operations describe ${f##* } --location=${CLUSTER_LOCATION}"
      status=3
    done
    [ "${#in_progress[@]}" -eq 0 ] || echo "   then rerun the check: $0 --check"
  fi
  exit "${status}"
}

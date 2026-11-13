# Shared by deploy/gke/teardown.sh, park.sh and resume.sh (sourced, not run).
# Requires the variables of deploy/gke/env.sh.example; changes nothing by itself.

for var in PROJECT_ID PROJECT_NUMBER REGION ZONE CLUSTER_NAME SYSTEM_POOL WORKER_POOL \
  BUCKET_NAME AR_REPO NODE_GSA_NAME SNAPSHOT_TRANSFER_GSA_NAME BILLING_ACCOUNT BUDGET_NAME \
  BASTION_NAME; do
  if [ -z "${!var:-}" ]; then
    echo "${var} is not set: source deploy/gke/env.sh first" >&2
    exit 2
  fi
done
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
  "${gc[@]}" container clusters describe "${CLUSTER_NAME}" --location="${ZONE}" \
    --format='value(name)' >/dev/null 2>&1
}

credentials() {
  "${gc[@]}" container clusters get-credentials "${CLUSTER_NAME}" --location="${ZONE}" \
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
Artifact Registry repositories|"\${gc[@]}" artifacts repositories list --location="\${REGION}" --format='value(name)'
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

finish() {
  local status="$1" f
  for f in "${failed[@]}"; do
    echo "FAILED: ${f}" >&2
    status=1
  done
  exit "${status}"
}

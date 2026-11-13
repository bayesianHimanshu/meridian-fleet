#!/usr/bin/env bash
# deploy/gke/run.sh: run a meridian command against the GKE cluster (docs/gke-session.md).
# It only sets MERIDIAN_CONFIG_OVERLAY=deploy/gke/meridian.toml ([substrate] target gke:
# the GKE context, Artifact Registry, deploy/gke/substrate and deploy/gke/dispatcher) and
# checks that kubectl's context exists. Example: deploy/gke/run.sh dispatcher status
set -euo pipefail
cd "$(dirname "$0")/../.."
export MERIDIAN_CONFIG_OVERLAY=deploy/gke/meridian.toml
context="$(sed -n 's/^context = "\(.*\)"$/\1/p' deploy/gke/meridian.toml)"
if ! kubectl config get-contexts -o name | grep -qx "${context}"; then
  echo "no kubectl context ${context}: run gcloud container clusters get-credentials" \
    "(docs/gke-session.md, step 9)" >&2
  exit 2
fi
exec uv run meridian "$@"

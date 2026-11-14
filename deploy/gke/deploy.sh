#!/usr/bin/env bash
# deploy/gke/deploy.sh: Meridian's side on the GKE cluster, once Substrate is installed
# (docs/gke-session.md, part 2, step 13). Every step goes through deploy/gke/run.sh, so
# [substrate] target is gke; nothing here creates a Google Cloud resource: the images are
# the ones you pushed to Artifact Registry (read by digest), and the objects created are
# Kubernetes ones (namespaces, the WorkerPool, the dispatcher, Substrate's templates).
#
#   token      the shared token: ~/.config/meridian/dispatcher.token (0600), never in git
#   substrate  agent image by digest, deploy/gke/substrate rendered and committed by you,
#              WorkerPool on the Spot pool (label and toleration), the atespace and its
#              access policy (the dispatcher only), templates, golden snapshots
#   dispatcher its image by digest, deploy/gke/dispatcher rendered, applied with the token
#              Secret; checks the router admits only the dispatcher and that no IAM grant
#              names it (Workload Identity: no role); waits until Ready
#   placement  workers only on the labelled, tainted Spot nodes; a pod without the
#              toleration refused there; the dispatcher off the worker nodes
#   status     the dispatcher through one port-forward over the DNS endpoint
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
run="${here}/run.sh"
echo "== token";      "${run}" dispatcher token
echo "== substrate";  "${run}" substrate deploy
echo "== dispatcher"; "${run}" dispatcher deploy
echo "== placement";  "${run}" substrate placement-check
echo "== status";     "${run}" dispatcher status
echo "deploy done: commit deploy/gke/substrate and deploy/gke/dispatcher (the digests used)"

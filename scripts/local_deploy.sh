#!/usr/bin/env bash
# Build, load and deploy the services to the local minikube cluster.
#
# The reason this is a script rather than a README paragraph is one specific
# trap, hit for real:
#
#   `minikube image load medw-gateway:dev` followed by `helm upgrade` does NOT
#   give you the new code. The tag is unchanged, so the kubelet already has an
#   image by that name and `pullPolicy: Never` tells it not to look further.
#   The pod restarts, reports healthy, and runs the OLD binary. Nothing errors.
#
# That is exactly the argument the charts already make about `:latest` -
# a mutable tag makes "what is running" unanswerable - arriving locally instead
# of in production. The fix is the same one production uses: an immutable tag
# per build. Here it is a timestamp rather than a git SHA, because the working
# tree is usually dirty during local iteration and a SHA would then be a lie.
#
#   ./scripts/local_deploy.sh              # build, load, deploy everything
#   ./scripts/local_deploy.sh gateway      # just one service
set -euo pipefail

CLUSTER=${CLUSTER:-medw}
NS=${NS:-medw}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TAG="dev-$(date +%s)"
SERVICES=("${@:-gateway retrieval generation ingestion_worker}")
read -ra SERVICES <<< "${SERVICES[*]}"

echo "==> tag: $TAG (immutable; a reused tag is invisible to the kubelet)"

for svc in "${SERVICES[@]}"; do
  chart="${svc//_/-}"
  image="medw-${chart}"
  echo "==> $svc"
  docker build -q -f "services/$svc/Dockerfile" -t "$image:$TAG" . >/dev/null
  minikube image load "$image:$TAG" -p "$CLUSTER"
  helm upgrade --install "$chart" "deploy/charts/$chart" \
    -n "$NS" --kube-context "$CLUSTER" \
    -f "deploy/charts/$chart/values-local.yaml" \
    --set "image.tag=$TAG" \
    --wait --timeout 180s
done

echo
kubectl --context "$CLUSTER" get pods -n "$NS"
echo
echo "Old images accumulate in the cluster. To reclaim:"
echo "  minikube image ls -p $CLUSTER | grep medw- "
echo "  minikube image rm <image> -p $CLUSTER"

#!/usr/bin/env bash
set -euo pipefail

ENVOY_GATEWAY_VERSION=v1.9.2
CLUSTER=bookreviews
NAMESPACE=bookreviews
HOST=bookreviews.localtest.me
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(cd "$HERE/../../.." && pwd)

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config "$HERE/kind-cluster.yaml"
fi
kubectl config use-context "kind-$CLUSTER"

kubectl apply --server-side --force-conflicts \
  -f "https://github.com/envoyproxy/gateway/releases/download/$ENVOY_GATEWAY_VERSION/install.yaml"
kubectl -n envoy-gateway-system wait deployment/envoy-gateway --for=condition=Available --timeout=5m
kubectl apply -f "$HERE/gatewayclass.yaml"

docker build --tag bookreviews:local "$PROJECT"
for image in bookreviews:local mariadb:11.3.2 rabbitmq:3.13.0-management; do
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
  kind load docker-image --name "$CLUSTER" "$image"
done

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
certificates=$(mktemp -d)
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$HOST" \
  -addext "subjectAltName=DNS:$HOST" \
  -keyout "$certificates/tls.key" -out "$certificates/tls.crt" 2>/dev/null
kubectl -n "$NAMESPACE" create secret tls bookreviews-tls \
  --cert "$certificates/tls.crt" --key "$certificates/tls.key" --dry-run=client -o yaml | kubectl apply -f -
rm -rf "$certificates"

kubectl -n "$NAMESPACE" delete job bookreviews-migrate --ignore-not-found
kubectl apply -k "$PROJECT/deploy/kubernetes/overlays/local"
kubectl -n "$NAMESPACE" wait job/bookreviews-migrate --for=condition=Complete --timeout=10m
kubectl -n "$NAMESPACE" rollout status deployment/bookreviews-api --timeout=5m
kubectl -n "$NAMESPACE" rollout status deployment/bookreviews-worker --timeout=5m
kubectl -n "$NAMESPACE" wait gateway/bookreviews --for=condition=Programmed --timeout=5m

echo
echo "Ready. Reach the API through the gateway with:"
echo "  kubectl -n envoy-gateway-system port-forward service/\$(kubectl -n envoy-gateway-system get service -l gateway.envoyproxy.io/owning-gateway-name=bookreviews -o name | cut -d/ -f2) 8443:443"
echo "  curl -k --resolve $HOST:8443:127.0.0.1 https://$HOST:8443/book/search?q=austen"

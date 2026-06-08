#!/usr/bin/env bash
# Install ARC + the runner scale set on the OKE cluster. Run ON amitdemografana
# (in-VCN, reaches the private API endpoint). Requires: oci CLI auth, kubectl,
# helm, and oke/config.env + oke/state.env present alongside this script.
# Reads the PAT from the Vault secret; NEVER prints it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a

# --- 1. kubeconfig for the private endpoint ---
export KUBECONFIG="$HOME/.kube/config-aidp-cicd"
oci ce cluster create-kubeconfig --cluster-id "$CLUSTER" --region "$REGION" \
  --file "$KUBECONFIG" --token-version 2.0.0 --kube-endpoint PRIVATE_ENDPOINT
kubectl get nodes

# --- 2. namespaces + service account ---
kubectl apply -f "$HERE/namespaces.yaml"
kubectl apply -f "$HERE/runner-serviceaccount.yaml"

# --- 3. PAT secret (from Vault; value via stdin, never echoed) ---
PAT="$(oci secrets secret-bundle get --secret-id "$PAT_SECRET_OCID" --region "$REGION" \
  --query "data.\"secret-bundle-content\".content" --raw-output | base64 -d)"
kubectl create secret generic aidp-cicd-pat -n "$RUNNER_NAMESPACE" \
  --from-literal=github_token="$PAT" --dry-run=client -o yaml | kubectl apply -f -
unset PAT

# --- 4. ARC controller ---
helm upgrade --install arc \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
  --namespace "$ARC_SYSTEMS_NAMESPACE" --create-namespace \
  -f "$HERE/values-controller.yaml"
kubectl -n "$ARC_SYSTEMS_NAMESPACE" rollout status deploy --timeout=180s

# --- 5. runner scale set (release name == $RUNNER_SCALE_SET == runs-on value) ---
helm upgrade --install "$RUNNER_SCALE_SET" \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
  --namespace "$RUNNER_NAMESPACE" \
  -f "$HERE/values-runnerset.yaml"

echo "=== bootstrap complete; verify with: kubectl get pods -A | grep -E 'arc|runner' ==="

#!/usr/bin/env bash
# Pre-create the AIDP GIT_ACCOUNT credential (git.credential_name) under OKE
# Workload Identity, sourcing the PAT from the aidp-cicd-pat k8s Secret (which
# bootstrap-runner.sh populated from the OCI Vault secret via the bastion's
# instance principal).
#
# Why this exists: aidp_deploy.py phase 0 normally reconciles the credential
# straight from the OCI secret, but that needs the *running* principal to have
# `read secret-bundles`. The OKE workload DG may lack that policy, so phase 0
# can't create it on OKE. This script creates it once under Workload Identity
# (PAT delivered via the k8s Secret, not the workload's own secret read); phase 0
# then finds the existing setting and proceeds. The VM path (instance principal
# that CAN read the secret) needs none of this.
#
# Run ON amitdemografana after bootstrap-runner.sh. NEVER prints the PAT.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a
export KUBECONFIG="$HOME/.kube/config-aidp-cicd"
export OCI_CLI_AUTH="${OCI_CLI_AUTH:-instance_principal}"

# driver: under WI, create the GIT_ACCOUNT setting from env GIT_PAT if absent
cat >/tmp/mkcred.py <<'PY'
import os, sys
sys.path.insert(0, "/tmp/repo/deploy")
os.environ["AIDP_AUTH_METHOD"] = "oke_workload_identity"
import aidp_deploy as A
cfg = A.load_config("/tmp/repo/deploy/cicd.yaml")
client = A.AidpClient(cfg, A.build_signer())
g = cfg["git"]; name = g["credential_name"]; pat = os.environ["GIT_PAT"]
existing = A._find_setting_key_by_name(client.list_git_account_settings(), name)
if existing:
    print("git credential already exists:", name, existing); sys.exit(0)
body = {"name": name, "isDefault": False,
        "data": {"type": "GIT_ACCOUNT", "providerName": "GITHUB",
                 "entityType": "PERSONAL_ACCESS_TOKEN",
                 "username": g["credential_username"], "personalAccessToken": pat}}
r = client.request_ok("POST", client.lake_url("userSettings"), body=body)
print("created git credential:", name, r.json().get("key"))
PY

kubectl -n "$RUNNER_NAMESPACE" create configmap mkcred-py \
  --from-file=mkcred.py=/tmp/mkcred.py --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$RUNNER_NAMESPACE" delete pod mkcred --ignore-not-found
cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: mkcred
  namespace: $RUNNER_NAMESPACE
spec:
  serviceAccountName: $RUNNER_SA
  restartPolicy: Never
  containers:
    - name: mkcred
      image: ghcr.io/actions/actions-runner:latest
      command: ["bash","-lc"]
      args:
        - >
          set -e;
          sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip git >/dev/null;
          git clone -q -b feat/oke-runner "$GITHUB_CONFIG_URL" /tmp/repo;
          pip3 install -q --break-system-packages oci requests pyyaml;
          python3 /mkcred.py
      env:
        - name: GIT_PAT
          valueFrom:
            secretKeyRef:
              name: aidp-cicd-pat
              key: github_token
      resources:
        requests: { cpu: "500m", memory: "1Gi" }
        limits:   { cpu: "500m", memory: "1Gi" }
      volumeMounts:
        - name: mkcred
          mountPath: /mkcred.py
          subPath: mkcred.py
  volumes:
    - name: mkcred
      configMap:
        name: mkcred-py
YAML
kubectl -n "$RUNNER_NAMESPACE" wait --for=condition=Ready pod/mkcred --timeout=180s || true
kubectl -n "$RUNNER_NAMESPACE" logs -f mkcred
echo "Verify: kubectl -n $RUNNER_NAMESPACE logs mkcred | grep -E 'created|already exists'"

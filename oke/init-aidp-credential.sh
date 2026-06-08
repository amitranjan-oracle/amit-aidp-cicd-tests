#!/usr/bin/env bash
# Create AIDP GIT_ACCOUNT credential 'cicd-workload-principal' OWNED BY the OKE
# workload principal, by running a one-off pod under aidp-runner-sa. Run on
# amitdemografana after bootstrap-runner.sh. NEVER prints the PAT.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a
export KUBECONFIG="$HOME/.kube/config-aidp-cicd"

# driver script that creates the GIT_ACCOUNT setting under the WI signer
cat >/tmp/mkcred.py <<'PY'
import os, sys
sys.path.insert(0, "/work/deploy")
os.environ["AIDP_AUTH_METHOD"] = "oke_workload_identity"
import aidp_deploy as A
cfg = A.load_config("/work/deploy/cicd.yaml")
client = A.AidpClient(cfg, A.build_signer())
name = os.environ["CRED_NAME"]; pat = os.environ["GIT_PAT"]
existing = A._find_setting_key_by_name(client.list_git_account_settings(), name)
if existing:
    print("already exists:", existing); sys.exit(0)
body = {"name": name, "isDefault": False,
        "data": {"type": "GIT_ACCOUNT", "providerName": "GITHUB",
                 "entityType": "PERSONAL_ACCESS_TOKEN",
                 "username": "amitranjan-oracle", "personalAccessToken": pat}}
r = client.request_ok("POST", client.lake_url("userSettings"), body=body)
print("created key:", r.json().get("key"))
PY

# ship the driver as a ConfigMap so it can be mounted into the pod
kubectl -n "$RUNNER_NAMESPACE" create configmap mkcred-py \
  --from-file=mkcred.py=/tmp/mkcred.py --dry-run=client -o yaml | kubectl apply -f -

# one-off pod under the WI ServiceAccount: clone repo, install deps, run driver
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
          git clone -q "$GITHUB_CONFIG_URL" /work;
          pip3 install -q oci requests pyyaml;
          CRED_NAME="$AIDP_GIT_CREDENTIAL_NAME" python3 /mkcred.py
      env:
        - name: GIT_PAT
          valueFrom:
            secretKeyRef:
              name: aidp-cicd-pat
              key: github_token
      volumeMounts:
        - name: mkcred
          mountPath: /mkcred.py
          subPath: mkcred.py
  volumes:
    - name: mkcred
      configMap:
        name: mkcred-py
YAML
kubectl -n "$RUNNER_NAMESPACE" wait --for=condition=Ready pod/mkcred --timeout=90s || true
kubectl -n "$RUNNER_NAMESPACE" logs -f mkcred
echo "Verify: kubectl -n $RUNNER_NAMESPACE logs mkcred | grep -E 'created key|already exists'"

# OKE-Hosted GitHub Runner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a GitHub self-hosted runner in a minimal, private OKE Enhanced
cluster (same VCN/subnet as `amit-cicd-compute`) that runs the AIDP CI/CD
reconcile via **OKE Workload Identity**, coexisting with the VM runner.

**Architecture:** Provision an Enhanced OKE cluster (private API endpoint +
1-node pool in `10.0.1.0/24`, flannel CNI, OKE NSGs) via OCI CLI scripts. Install
GitHub-official ARC (`gha-runner-scale-set`) via Helm with a PAT (from Vault)
k8s Secret. Runner pods run under ServiceAccount `aidp-runner-sa`, whose workload
dynamic group (user-added to `AI_DATA_PLATFORM_ADMIN`) authorizes AIDP data-plane
calls. `deploy/aidp_deploy.py` gains a WI-signer branch + resolve-credential-by-name.

**Tech Stack:** OCI CLI 3.78, OKE (Enhanced, flannel), Helm 3, kubectl, ARC
`gha-runner-scale-set`, OCI Python SDK (WI signer), GitHub Actions, Python 3.9.

**Spec:** `docs/superpowers/specs/2026-06-08-oke-runner-design.md`

**Access for live steps:** `kubectl`/`helm`/`oci ce` run on `amitdemografana`
(`ssh -i /Users/amitranjan/OracleContent/ssh-keys/ssh-key-2025-08-29.key opc@144.25.95.237`),
which is in the same VCN and reaches the private API endpoint. OCI CLI cluster
provisioning (Tasks 9–10) runs from the Mac (DEFAULT profile).

**Secrets discipline:** the PAT (Vault `amitranjan-git-pat`) and its value are
NEVER echoed or committed. Read it into variables/stdin only.

---

## File Structure

| File | Responsibility |
|---|---|
| `deploy/aidp_deploy.py` (modify) | + `select_auth_method()`, WI signer branch, `_find_setting_key_by_name()`, `AidpClient.list_git_account_settings()` / `resolve_git_credential_key()`; phase2 uses resolved key |
| `deploy/test_aidp_deploy_oke.py` (create) | stdlib `unittest` for the new pure logic (no OCI/network) |
| `oke/config.env` (create) | committed inputs: OCIDs, names, CIDRs |
| `oke/state.env` (gitignored) | derived OCIDs written by scripts (cluster, NSGs, DG) |
| `oke/namespaces.yaml` (create) | `arc-systems`, `arc-runners` namespaces |
| `oke/runner-serviceaccount.yaml` (create) | `aidp-runner-sa` (WI subject) |
| `oke/values-controller.yaml` (create) | ARC controller Helm values |
| `oke/values-runnerset.yaml` (create) | runner scale-set Helm values (SA, PAT secret, scale set name) |
| `oke/provision-cluster.sh` (create) | NSGs + Enhanced cluster + node pool |
| `oke/create-workload-dg.sh` (create) | workload dynamic group → prints OCID |
| `oke/bootstrap-runner.sh` (create) | kubeconfig, ns/SA, PAT secret, helm install controller + runner set |
| `oke/init-aidp-credential.sh` (create) | WI pod creates `cicd-workload-principal` from Vault PAT |
| `.github/workflows/cicd-oke.yml` (create) | `workflow_dispatch`; `runs-on: amit-cicd-oke`; setup-python; reconcile |
| `docs/oke-runner-setup.md` (create) | README/runbook tying it all together + as-built |
| `.gitignore` (modify) | add `oke/state.env` |

---

## Task 1: `aidp_deploy.py` — WI signer + resolve-credential-by-name (TDD)

**Files:**
- Create: `deploy/test_aidp_deploy_oke.py`
- Modify: `deploy/aidp_deploy.py` (`build_signer` ~124-148; `phase2_git_folder` ~378-391; add module fns + `AidpClient` methods)

- [ ] **Step 1: Write the failing test**

Create `deploy/test_aidp_deploy_oke.py`:

```python
#!/usr/bin/env python3
"""Unit tests for the OKE additions to aidp_deploy (no OCI/network needed)."""
import os
import unittest

import aidp_deploy as A


class SelectAuthMethod(unittest.TestCase):
    def test_default_is_instance_principal(self):
        self.assertEqual(A.select_auth_method({}), "instance_principal")

    def test_resource_principal_env(self):
        self.assertEqual(
            A.select_auth_method({"OCI_RESOURCE_PRINCIPAL_VERSION": "2.2"}),
            "resource_principal")

    def test_oke_wi_wins_over_rp(self):
        env = {"AIDP_AUTH_METHOD": "oke_workload_identity",
               "OCI_RESOURCE_PRINCIPAL_VERSION": "2.2"}
        self.assertEqual(A.select_auth_method(env), "oke_workload_identity")

    def test_oke_wi_case_insensitive(self):
        self.assertEqual(
            A.select_auth_method({"AIDP_AUTH_METHOD": "OKE_Workload_Identity"}),
            "oke_workload_identity")


class FindSettingKeyByName(unittest.TestCase):
    def test_found(self):
        settings = [{"name": "other", "key": "K0"},
                    {"name": "cicd-workload-principal", "key": "K1"}]
        self.assertEqual(
            A._find_setting_key_by_name(settings, "cicd-workload-principal"), "K1")

    def test_absent_returns_none(self):
        self.assertIsNone(A._find_setting_key_by_name([{"name": "x", "key": "K"}], "y"))


class ResolveGitCredentialKey(unittest.TestCase):
    def _client(self):
        cfg = {"aidp": {"region": "r", "data_lake_ocid": "d", "path_prefix": "p",
                        "api_version": "1", "workspace_key": "w"},
               "git": {"credential_key": "YAML_KEY"}}
        return A.AidpClient(cfg, signer=None), cfg

    def setUp(self):
        os.environ.pop("AIDP_GIT_CREDENTIAL_NAME", None)

    def tearDown(self):
        os.environ.pop("AIDP_GIT_CREDENTIAL_NAME", None)

    def test_no_env_uses_yaml_key(self):
        client, cfg = self._client()
        client.list_git_account_settings = lambda: (_ for _ in ()).throw(
            AssertionError("must not query when env unset"))
        self.assertEqual(client.resolve_git_credential_key(cfg), "YAML_KEY")

    def test_env_resolves_by_name(self):
        client, cfg = self._client()
        os.environ["AIDP_GIT_CREDENTIAL_NAME"] = "cicd-workload-principal"
        client.list_git_account_settings = lambda: [
            {"name": "cicd-workload-principal", "key": "WI_KEY"}]
        self.assertEqual(client.resolve_git_credential_key(cfg), "WI_KEY")

    def test_env_name_missing_raises(self):
        client, cfg = self._client()
        os.environ["AIDP_GIT_CREDENTIAL_NAME"] = "absent"
        client.list_git_account_settings = lambda: [{"name": "x", "key": "K"}]
        with self.assertRaises(RuntimeError):
            client.resolve_git_credential_key(cfg)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `cd deploy && python3 -m unittest test_aidp_deploy_oke -v`
Expected: FAIL — `AttributeError: module 'aidp_deploy' has no attribute 'select_auth_method'`.

- [ ] **Step 3: Add `select_auth_method()` and rewrite `build_signer()`**

In `deploy/aidp_deploy.py`, replace the whole `build_signer()` function (lines ~124-148) with:

```python
def select_auth_method(env: Dict[str, str]) -> str:
    """Pick the OCI signer to build, by priority:
    OKE workload identity (explicit) > resource principal (env) > instance principal.
    Pure function over an environment mapping so it is unit-testable.
    """
    if env.get("AIDP_AUTH_METHOD", "").strip().lower() == "oke_workload_identity":
        return "oke_workload_identity"
    if env.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
        return "resource_principal"
    return "instance_principal"


def build_signer():
    """Build an OCI signer per select_auth_method(); all subclass
    requests.auth.AuthBase so they plug into requests as auth=.
    Raises RuntimeError with a clear message if none is available.
    """
    import os
    try:
        import oci
    except ImportError as exc:
        raise RuntimeError(
            "oci SDK not importable ({}); run this where oci is installed.".format(exc))
    method = select_auth_method(os.environ)
    if method == "oke_workload_identity":
        log.info("Using OKE workload-identity signer.")
        return oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
    if method == "resource_principal":
        log.info("Using resource-principal signer.")
        return oci.auth.signers.get_resource_principals_signer()
    try:
        log.info("Using instance-principal signer.")
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    except Exception as exc:  # not on OCI / IMDS unreachable
        raise RuntimeError(
            "No OCI principal available (instance-principal init failed: {}). "
            "Run this on the OCI box / OKE pod.".format(exc))
```

- [ ] **Step 4: Add `_find_setting_key_by_name()` (module level, near `_async_key`)**

Insert after `_async_key()` (~line 154):

```python
def _find_setting_key_by_name(settings: List[Dict[str, Any]], name: str) -> Optional[str]:
    """Return the `key` of the userSetting whose `name` matches, else None."""
    for s in settings:
        if s.get("name") == name:
            return s.get("key")
    return None
```

- [ ] **Step 5: Add `AidpClient.list_git_account_settings()` + `resolve_git_credential_key()`**

Insert into `AidpClient` (e.g. just before `# ---- Phase 1: directory ----`, ~line 220):

```python
    # ---- credentials (GIT_ACCOUNT userSettings, resolved in caller identity) ----
    def list_git_account_settings(self) -> List[Dict[str, Any]]:
        data = self.request_ok("GET", self.lake_url("userSettings"),
                               params={"settingType": "GIT_ACCOUNT"}).json()
        return data.get("items", data) if isinstance(data, dict) else data

    def resolve_git_credential_key(self, cfg: Dict[str, Any]) -> str:
        """If AIDP_GIT_CREDENTIAL_NAME is set, resolve the GIT_ACCOUNT key by name
        under the current principal (so a freshly-minted key need not be copied
        into config); otherwise use the yaml git.credential_key. Used on OKE where
        the credential is owned by the workload principal."""
        import os
        name = os.environ.get("AIDP_GIT_CREDENTIAL_NAME", "").strip()
        if not name:
            return cfg["git"]["credential_key"]
        settings = self.list_git_account_settings()
        key = _find_setting_key_by_name(settings, name)
        if not key:
            raise RuntimeError(
                "GIT_ACCOUNT credential named {!r} not found under the current "
                "principal (visible: {}). Run oke/init-aidp-credential.sh first."
                .format(name, [s.get("name") for s in settings]))
        log.info("resolved git credential %r -> key %s", name, key)
        return key
```

- [ ] **Step 6: Use the resolved key in `phase2_git_folder`**

Replace the `else:` clone branch of `phase2_git_folder` (~lines 388-391) with:

```python
    else:
        log.info("git folder absent; cloning")
        cred_key = client.resolve_git_credential_key(cfg)
        client.wait_for_async(client.create_git_folder(
            g["folder_path"], g["repository_url"], g["branch"], cred_key))
```

(The pull branch is unchanged — pull needs no credential. The VM path is
unchanged: no `AIDP_GIT_CREDENTIAL_NAME` ⇒ yaml key; no `AIDP_AUTH_METHOD` ⇒
instance principal.)

- [ ] **Step 7: Run the tests, verify they pass**

Run: `cd deploy && python3 -m unittest test_aidp_deploy_oke -v`
Expected: PASS (9 tests OK).

- [ ] **Step 8: Regression — offline dry-run still works**

Run: `cd /Users/amitranjan/IdeaProjects/amit-aidp-cicd-tests && python3 deploy/aidp_deploy.py --config deploy/cicd.yaml --dry-run`
Expected: completes; logs `Using instance-principal signer` OR (off-box) the
offline-dry-run warning, then validates specs and `== Done ==`. No traceback.

- [ ] **Step 9: Commit**

```bash
git add deploy/aidp_deploy.py deploy/test_aidp_deploy_oke.py
git commit -m "feat(deploy): OKE workload-identity signer + resolve git credential by name"
```

---

## Task 2: `oke/config.env` + `.gitignore`

**Files:** Create `oke/config.env`; Modify `.gitignore`

- [ ] **Step 1: Create `oke/config.env`** (committed inputs — real discovered values)

```bash
# oke/config.env — inputs for the OKE runner scripts. Source it: `set -a; . oke/config.env; set +a`
# Derived OCIDs (cluster, NSGs, DG) are written by the scripts into oke/state.env (gitignored).

REGION=us-ashburn-1
COMPARTMENT_OCID=ocid1.compartment.oc1..aaaaaaaaxtf7gpp5elpwzjub5odf5dapcvrrvvnytuupdvsk4x2hgb5v5zva   # DataServices
VCN_OCID=ocid1.vcn.oc1.iad.amaaaaaaai22xpqaxoezzlk6wx2fyui4el6p453bpypzq5qoixtogr267glq                # dsvcn 10.0.0.0/16
SUBNET_OCID=ocid1.subnet.oc1.iad.aaaaaaaap3sh5hnpfkv4eecgolm7inquxdks3j255x4duajpquyid2ni7pba          # private subnet-dsvcn 10.0.1.0/24
AVAILABILITY_DOMAIN="yBdo:US-ASHBURN-AD-1"
BASTION_SUBNET_CIDR=10.0.0.0/24    # amitdemografana public subnet — kubectl source

K8S_VERSION=v1.34.2
NODE_SHAPE=VM.Standard.E4.Flex
NODE_OCPUS=1
NODE_MEM_GB=8
NODE_COUNT=1
NODE_BOOT_GB=50
POD_CIDR=10.244.0.0/16
SERVICE_CIDR=10.96.0.0/16

CLUSTER_NAME=aidp-cicd-test
NODE_POOL_NAME=aidp-cicd-test-np
API_NSG_NAME=aidp-cicd-test-api-nsg
NODE_NSG_NAME=aidp-cicd-test-node-nsg
WORKLOAD_DG_NAME=aidp-cicd-test-workload-dg

ARC_SYSTEMS_NAMESPACE=arc-systems
RUNNER_NAMESPACE=arc-runners
RUNNER_SA=aidp-runner-sa
RUNNER_SCALE_SET=amit-cicd-oke          # == runs-on value in cicd-oke.yml
GITHUB_CONFIG_URL=https://github.com/amitranjan-oracle/amit-aidp-cicd-tests
PAT_SECRET_OCID=ocid1.vaultsecret.oc1.iad.amaaaaaaai22xpqatzdboqsmngy72nhogsg32okj63o6h2ex2mwahvzfxqsq
AIDP_GIT_CREDENTIAL_NAME=cicd-workload-principal
```

- [ ] **Step 2: Add `oke/state.env` to `.gitignore`**

Append to `.gitignore`:
```
# OKE derived OCIDs (cluster/NSG/DG) — environment-specific, not committed
oke/state.env
```

- [ ] **Step 3: Commit**

```bash
git add oke/config.env .gitignore
git commit -m "chore(oke): config.env inputs + gitignore state.env"
```

---

## Task 3: Kubernetes manifests + Helm values

**Files:** Create `oke/namespaces.yaml`, `oke/runner-serviceaccount.yaml`,
`oke/values-controller.yaml`, `oke/values-runnerset.yaml`

- [ ] **Step 1: `oke/namespaces.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: arc-systems
---
apiVersion: v1
kind: Namespace
metadata:
  name: arc-runners
```

- [ ] **Step 2: `oke/runner-serviceaccount.yaml`**

```yaml
# The Workload-Identity subject. The workload dynamic group matches this SA in
# this namespace; it must exist before the runner scale set references it.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: aidp-runner-sa
  namespace: arc-runners
```

- [ ] **Step 3: `oke/values-controller.yaml`** (controller defaults are fine)

```yaml
# Helm values for gha-runner-scale-set-controller (chart defaults suffice for a
# single repo-scoped runner set). Kept as a file for reproducibility/overrides.
replicaCount: 1
```

- [ ] **Step 4: `oke/values-runnerset.yaml`**

```yaml
# Helm values for the gha-runner-scale-set chart. The Helm RELEASE NAME (set in
# bootstrap-runner.sh to $RUNNER_SCALE_SET=amit-cicd-oke) becomes the
# runner-scale-set name and the value used in `runs-on:`.
githubConfigUrl: "https://github.com/amitranjan-oracle/amit-aidp-cicd-tests"
githubConfigSecret: aidp-cicd-pat        # pre-created k8s Secret (PAT) — see bootstrap
minRunners: 0
maxRunners: 2
template:
  spec:
    serviceAccountName: aidp-runner-sa   # Workload-Identity subject
    containers:
      - name: runner
        image: ghcr.io/actions/actions-runner:latest
        command: ["/home/runner/run.sh"]
```

- [ ] **Step 5: Validate YAML parses**

Run: `python3 -c "import yaml,glob; [list(yaml.safe_load_all(open(f))) for f in glob.glob('oke/*.yaml')]; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add oke/namespaces.yaml oke/runner-serviceaccount.yaml oke/values-controller.yaml oke/values-runnerset.yaml
git commit -m "feat(oke): k8s manifests + ARC helm values"
```

---

## Task 4: `.github/workflows/cicd-oke.yml`

**Files:** Create `.github/workflows/cicd-oke.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: aidp-cicd-oke
on:
  workflow_dispatch: {}

# Coexists with cicd.yml (VM). Targets the OKE runner scale set by name.
jobs:
  reconcile:
    runs-on: amit-cicd-oke          # == the gha-runner-scale-set install name
    env:
      AIDP_AUTH_METHOD: oke_workload_identity
      AIDP_GIT_CREDENTIAL_NAME: cicd-workload-principal
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install Python deps
        run: pip install oci requests pyyaml
      - name: Preflight imports
        run: python3 -c "import oci, requests, yaml; print('deps ok', oci.__version__)"
      - name: Reconcile AIDP
        run: python3 deploy/aidp_deploy.py --config deploy/cicd.yaml
```

- [ ] **Step 2: Validate YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/cicd-oke.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit** (push later over SSH — workflow scope, see runner doc §6)

```bash
git add .github/workflows/cicd-oke.yml
git commit -m "feat(ci): OKE workflow (workflow_dispatch, WI auth, runs-on amit-cicd-oke)"
```

---

## Task 5: `oke/provision-cluster.sh`

**Files:** Create `oke/provision-cluster.sh` (run from Mac, DEFAULT profile)

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Provision a minimal private Enhanced OKE cluster in the VM's subnet.
# Idempotent-ish: skips create if a same-named resource already exists.
# Writes derived OCIDs to oke/state.env. Run from a host with OCI CLI + DEFAULT profile.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; set +a
STATE="$HERE/state.env"; : > "$STATE"
log(){ printf '\n=== %s ===\n' "$*"; }

# --- OSN service OCID (for service-gateway egress rules) ---
OSN_DEST=$(oci network service list --region "$REGION" \
  --query "data[?contains(\"cidr-block\",'all-')].\"cidr-block\" | [0]" --raw-output)
log "OSN dest = $OSN_DEST"

# --- 1. NSGs ---
get_nsg(){ oci network nsg list -c "$COMPARTMENT_OCID" --vcn-id "$VCN_OCID" --region "$REGION" \
  --display-name "$1" --query "data[0].id" --raw-output 2>/dev/null; }
API_NSG=$(get_nsg "$API_NSG_NAME"); [ "$API_NSG" = "null" ] && API_NSG=""
NODE_NSG=$(get_nsg "$NODE_NSG_NAME"); [ "$NODE_NSG" = "null" ] && NODE_NSG=""
if [ -z "$API_NSG" ]; then
  API_NSG=$(oci network nsg create -c "$COMPARTMENT_OCID" --vcn-id "$VCN_OCID" \
    --display-name "$API_NSG_NAME" --region "$REGION" --wait-for-state AVAILABLE \
    --query "data.id" --raw-output); fi
if [ -z "$NODE_NSG" ]; then
  NODE_NSG=$(oci network nsg create -c "$COMPARTMENT_OCID" --vcn-id "$VCN_OCID" \
    --display-name "$NODE_NSG_NAME" --region "$REGION" --wait-for-state AVAILABLE \
    --query "data.id" --raw-output); fi
echo "API_NSG=$API_NSG"   >> "$STATE"
echo "NODE_NSG=$NODE_NSG" >> "$STATE"
log "API_NSG=$API_NSG NODE_NSG=$NODE_NSG"

# --- 2. NSG rules (OKE flannel private-cluster matrix) ---
# Replace ALL rules each run (idempotent). protocol 6=TCP, 1=ICMP, all=all.
oci network nsg rules update --nsg-id "$API_NSG" --region "$REGION" --force --security-rules "$(cat <<JSON
[
 {"direction":"INGRESS","protocol":"6","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","tcpOptions":{"destinationPortRange":{"min":6443,"max":6443}},"description":"workers to k8s API"},
 {"direction":"INGRESS","protocol":"6","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","tcpOptions":{"destinationPortRange":{"min":12250,"max":12250}},"description":"workers to control plane"},
 {"direction":"INGRESS","protocol":"1","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU from workers"},
 {"direction":"INGRESS","protocol":"6","source":"$BASTION_SUBNET_CIDR","sourceType":"CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":6443,"max":6443}},"description":"kubectl from amitdemografana subnet"}
]
JSON
)"
oci network nsg rules update --nsg-id "$API_NSG" --region "$REGION" --force --direction EGRESS --security-rules "$(cat <<JSON
[
 {"direction":"EGRESS","protocol":"6","destination":"$NODE_NSG","destinationType":"NETWORK_SECURITY_GROUP","description":"control plane to workers (all TCP incl 10250)"},
 {"direction":"EGRESS","protocol":"1","destination":"$NODE_NSG","destinationType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU to workers"},
 {"direction":"EGRESS","protocol":"6","destination":"$OSN_DEST","destinationType":"SERVICE_CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":443,"max":443}},"description":"control plane to OCI services via SGW"}
]
JSON
)"
oci network nsg rules update --nsg-id "$NODE_NSG" --region "$REGION" --force --security-rules "$(cat <<JSON
[
 {"direction":"INGRESS","protocol":"all","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","description":"node to node"},
 {"direction":"INGRESS","protocol":"6","source":"$API_NSG","sourceType":"NETWORK_SECURITY_GROUP","description":"control plane to workers (all TCP incl 10250)"},
 {"direction":"INGRESS","protocol":"1","source":"$API_NSG","sourceType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU from control plane"},
 {"direction":"INGRESS","protocol":"1","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","icmpOptions":{"type":3,"code":4},"description":"path MTU from internet"}
]
JSON
)"
oci network nsg rules update --nsg-id "$NODE_NSG" --region "$REGION" --force --direction EGRESS --security-rules "$(cat <<JSON
[
 {"direction":"EGRESS","protocol":"all","destination":"$NODE_NSG","destinationType":"NETWORK_SECURITY_GROUP","description":"node to node"},
 {"direction":"EGRESS","protocol":"6","destination":"$API_NSG","destinationType":"NETWORK_SECURITY_GROUP","tcpOptions":{"destinationPortRange":{"min":6443,"max":6443}},"description":"workers to k8s API"},
 {"direction":"EGRESS","protocol":"6","destination":"$API_NSG","destinationType":"NETWORK_SECURITY_GROUP","tcpOptions":{"destinationPortRange":{"min":12250,"max":12250}},"description":"workers to control plane"},
 {"direction":"EGRESS","protocol":"1","destination":"$API_NSG","destinationType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU to control plane"},
 {"direction":"EGRESS","protocol":"6","destination":"$OSN_DEST","destinationType":"SERVICE_CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":443,"max":443}},"description":"workers to OCI services via SGW (image pull)"},
 {"direction":"EGRESS","protocol":"all","destination":"0.0.0.0/0","destinationType":"CIDR_BLOCK","description":"NAT egress: GitHub, ghcr.io, PyPI"}
]
JSON
)"
log "NSG rules applied"

# --- 3. Cluster (Enhanced, private endpoint, flannel) ---
CLUSTER=$(oci ce cluster list -c "$COMPARTMENT_OCID" --region "$REGION" --name "$CLUSTER_NAME" \
  --lifecycle-state ACTIVE --query "data[0].id" --raw-output 2>/dev/null || true)
[ "$CLUSTER" = "null" ] && CLUSTER=""
if [ -z "$CLUSTER" ]; then
  CLUSTER=$(oci ce cluster create \
    --compartment-id "$COMPARTMENT_OCID" --name "$CLUSTER_NAME" --vcn-id "$VCN_OCID" \
    --kubernetes-version "$K8S_VERSION" --type ENHANCED_CLUSTER \
    --endpoint-subnet-id "$SUBNET_OCID" --endpoint-nsg-ids "[\"$API_NSG\"]" \
    --endpoint-public-ip-enabled false \
    --pods-cidr "$POD_CIDR" --services-cidr "$SERVICE_CIDR" \
    --cluster-pod-network-options '[{"cniType":"FLANNEL_OVERLAY"}]' \
    --region "$REGION" --wait-for-state ACTIVE \
    --query "data.id" --raw-output)
fi
echo "CLUSTER=$CLUSTER" >> "$STATE"
log "CLUSTER=$CLUSTER"

# --- 4. Node image for k8s version + shape (OL8) ---
IMAGE=$(oci ce node-pool-options get --node-pool-option-id all --region "$REGION" \
  --query "data.sources[?contains(\"source-name\",'OKE') && contains(\"source-name\",'$K8S_VERSION') && contains(\"source-name\",'aarch')==\`false\` && contains(\"source-name\",'GPU')==\`false\`].\"image-id\" | [0]" --raw-output)
log "node image = $IMAGE"
echo "NODE_IMAGE=$IMAGE" >> "$STATE"

# --- 5. Node pool (1 node, small) ---
NP=$(oci ce node-pool list -c "$COMPARTMENT_OCID" --cluster-id "$CLUSTER" --region "$REGION" \
  --name "$NODE_POOL_NAME" --query "data[0].id" --raw-output 2>/dev/null || true)
[ "$NP" = "null" ] && NP=""
if [ -z "$NP" ]; then
  NP=$(oci ce node-pool create \
    --cluster-id "$CLUSTER" --compartment-id "$COMPARTMENT_OCID" --name "$NODE_POOL_NAME" \
    --kubernetes-version "$K8S_VERSION" --node-shape "$NODE_SHAPE" \
    --node-shape-config "{\"ocpus\":$NODE_OCPUS,\"memoryInGBs\":$NODE_MEM_GB}" \
    --node-image-id "$IMAGE" --size "$NODE_COUNT" \
    --node-boot-volume-size-in-gbs "$NODE_BOOT_GB" \
    --placement-configs "[{\"availabilityDomain\":\"$AVAILABILITY_DOMAIN\",\"subnetId\":\"$SUBNET_OCID\"}]" \
    --nsg-ids "[\"$NODE_NSG\"]" \
    --node-pool-pod-network-option-details '{"cniType":"FLANNEL_OVERLAY"}' \
    --region "$REGION" --query "data.id" --raw-output)
fi
echo "NODE_POOL=$NP" >> "$STATE"
log "NODE_POOL=$NP — provisioning nodes (poll: oci ce node-pool get --node-pool-id $NP)"
log "Done. state.env:"; cat "$STATE"
```

- [ ] **Step 2: Lint the script**

Run: `bash -n oke/provision-cluster.sh && chmod +x oke/provision-cluster.sh && echo ok`
Expected: `ok` (syntax valid). *Do not run it yet — that's Task 9.*

- [ ] **Step 3: Commit**

```bash
git add oke/provision-cluster.sh
git commit -m "feat(oke): provision-cluster.sh (NSGs + enhanced cluster + node pool)"
```

---

## Task 6: `oke/create-workload-dg.sh`

**Files:** Create `oke/create-workload-dg.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Create the OKE Workload-Identity dynamic group and print its OCID.
# Requires oke/state.env (CLUSTER). Run from a host with OCI CLI + DEFAULT profile.
# The matching-rule attribute keys are validated by the API; if create is
# rejected with "invalid attribute", adjust per current OKE Workload-Identity docs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a
TENANCY=$(oci iam compartment list --query "data[0].\"compartment-id\"" --raw-output 2>/dev/null \
  || oci iam availability-domain list --query "data[0].\"compartment-id\"" --raw-output)

RULE="ALL {resource.type = 'workload', resource.compartment.id = '$COMPARTMENT_OCID', resource.k8s.cluster.id = '$CLUSTER', resource.k8s.namespace.name = '$RUNNER_NAMESPACE', resource.k8s.serviceaccount.name = '$RUNNER_SA'}"

EXIST=$(oci iam dynamic-group list --region "$REGION" --query "data[?\"name\"=='$WORKLOAD_DG_NAME'].id | [0]" --raw-output 2>/dev/null || true)
[ "$EXIST" = "null" ] && EXIST=""
if [ -n "$EXIST" ]; then
  DG="$EXIST"
  oci iam dynamic-group update --dynamic-group-id "$DG" --matching-rule "$RULE" --force >/dev/null
else
  DG=$(oci iam dynamic-group create --name "$WORKLOAD_DG_NAME" \
    --description "OKE workload identity for AIDP CI/CD runner ($RUNNER_NAMESPACE/$RUNNER_SA)" \
    --matching-rule "$RULE" --query "data.id" --raw-output)
fi
echo "WORKLOAD_DG=$DG" >> "$HERE/state.env"
cat <<EOF

=== Workload dynamic group ready ===
Name : $WORKLOAD_DG_NAME
OCID : $DG
Rule : $RULE

ACTION REQUIRED (user): add this OCID to AI_DATA_PLATFORM_ADMIN as a GROUP member:
  MCP: aidp_roles add_member role_key=AI_DATA_PLATFORM_ADMIN member_type=GROUP target=$DG
Until then, AIDP calls from runner pods will be unauthorized (401/403).
EOF
```

- [ ] **Step 2: Lint**

Run: `bash -n oke/create-workload-dg.sh && chmod +x oke/create-workload-dg.sh && echo ok`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add oke/create-workload-dg.sh
git commit -m "feat(oke): create-workload-dg.sh (WI dynamic group, prints OCID)"
```

---

## Task 7: `oke/bootstrap-runner.sh`

**Files:** Create `oke/bootstrap-runner.sh` (run on `amitdemografana`)

- [ ] **Step 1: Write the script**

```bash
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
```

- [ ] **Step 2: Lint**

Run: `bash -n oke/bootstrap-runner.sh && chmod +x oke/bootstrap-runner.sh && echo ok`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add oke/bootstrap-runner.sh
git commit -m "feat(oke): bootstrap-runner.sh (kubeconfig, ns/SA, PAT secret, ARC + runner set)"
```

---

## Task 8: `oke/init-aidp-credential.sh`

**Files:** Create `oke/init-aidp-credential.sh` (run on `amitdemografana`)

- [ ] **Step 1: Write the script**

Creates the `cicd-workload-principal` GIT_ACCOUNT setting **from a pod under
Workload Identity** (so AIDP records the workload principal as owner). The PAT is
read from Vault by the pod's WI principal (requires the workload DG to also have
an IAM policy permitting `read secret-bundles` — documented in the README) OR is
injected via the existing k8s Secret. We use the **k8s Secret** path (no extra
IAM policy) by mounting `aidp-cicd-pat` and POSTing to AIDP from a python pod.

```bash
#!/usr/bin/env bash
# Create AIDP GIT_ACCOUNT credential 'cicd-workload-principal' OWNED BY the OKE
# workload principal, by running a one-off pod under aidp-runner-sa. Run on
# amitdemografana after bootstrap-runner.sh. NEVER prints the PAT.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a
export KUBECONFIG="$HOME/.kube/config-aidp-cicd"

# Inline reconcile script + a tiny driver that creates the GIT_ACCOUNT setting
# under the WI signer. The pod gets the repo via git clone (public repo) and the
# PAT from the aidp-cicd-pat secret as env GIT_PAT.
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
```

- [ ] **Step 2: Lint**

Run: `bash -n oke/init-aidp-credential.sh && chmod +x oke/init-aidp-credential.sh && echo ok`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add oke/init-aidp-credential.sh
git commit -m "feat(oke): init-aidp-credential.sh (WI pod creates cicd-workload-principal)"
```

---

## Task 9 (LIVE): Provision the cluster

**Pre:** Tasks 1–8 committed. Cost starts when nodes run.

- [ ] **Step 1: Run provisioning** (from Mac)

Run: `cd /Users/amitranjan/IdeaProjects/amit-aidp-cicd-tests && ./oke/provision-cluster.sh`
Expected: prints `API_NSG`, `NODE_NSG`, `CLUSTER`, `NODE_IMAGE`, `NODE_POOL`;
writes `oke/state.env`. Cluster create waits for ACTIVE (~10–15 min).

- [ ] **Step 2: Wait for the node to be ACTIVE**

Run: `source oke/state.env; oci ce node-pool get --node-pool-id "$NODE_POOL" --query 'data.nodes[].{name:name,state:"lifecycle-state"}' --output table`
Expected (poll until): node `lifecycle-state` = `ACTIVE`.

- [ ] **Step 3: Exit criteria**

Cluster ACTIVE, node pool with 1 ACTIVE node, `oke/state.env` has all 5 OCIDs.
If node never leaves `CREATING`/`UPDATING`: check NSG rules (node↔control-plane
6443/12250/10250) — wrong ports here prevent node join.

---

## Task 10 (LIVE): Workload dynamic group → user role-add

- [ ] **Step 1: Create the DG** (from Mac)

Run: `./oke/create-workload-dg.sh`
Expected: prints `WORKLOAD_DG=<ocid>` and the ACTION REQUIRED block.

- [ ] **Step 2: Hand the OCID to the user**

Surface the DG OCID and the exact role-add instruction. **Gate:** Tasks 12–13's
AIDP calls stay unauthorized until the user adds it to `AI_DATA_PLATFORM_ADMIN`.
Report this step as *pending the role add*, never as passed, until confirmed.

---

## Task 11 (LIVE): Bootstrap ARC on the cluster

**Pre:** `amitdemografana` has `oci` CLI (auth), `kubectl`, `helm`. Copy
`oke/` + `deploy/` there (e.g. `scp -i <key> -r oke deploy opc@144.25.95.237:~/aidp-cicd/`),
or `git clone` the repo there.

- [ ] **Step 1: Verify tooling on amitdemografana**

Run (on box): `command -v kubectl helm oci || echo MISSING`
If MISSING: install kubectl (curl from dl.k8s.io), helm (get.helm.sh), and ensure
`oci` CLI auth works (`oci ce cluster list -c <comp> --region us-ashburn-1`).
Document whatever was installed in the README.

- [ ] **Step 2: Run bootstrap** (on box)

Run: `./oke/bootstrap-runner.sh`
Expected: `kubectl get nodes` shows Ready; controller rollout completes; runner
scale set installed.

- [ ] **Step 3: Exit criteria**

- `kubectl get pods -n arc-systems` → controller `Running`.
- `kubectl get pods -n arc-runners` → a listener pod `Running`.
- GitHub → repo Settings → Actions → Runners shows scale set `amit-cicd-oke`
  online (idle, 0 runners — ephemeral).
- `kubectl get autoscalingrunnerset -n arc-runners` exists.

---

## Task 12 (LIVE): Create `cicd-workload-principal` under WI

**Pre:** Task 10 role-add CONFIRMED by user (else this 401s). Task 11 done.

- [ ] **Step 1: Run** (on box)

Run: `./oke/init-aidp-credential.sh` (per the README's ConfigMap-mount form)
Expected: pod logs `created key: <key>` (or `already exists`).

- [ ] **Step 2: Verify ownership/visibility**

Run a WI pod that lists GIT_ACCOUNT settings; expect `cicd-workload-principal`
present. (If absent → the role-add hasn't propagated or WI signer failed; capture
the error, don't assume principal lacks privilege.)

---

## Task 13 (LIVE): End-to-end run + AIDP verification

**Pre:** workflow on `main` (push `cicd-oke.yml` over SSH + merge), Tasks 11–12 done.

- [ ] **Step 1: Dispatch the workflow**

Run: `gh workflow run aidp-cicd-oke --repo amitranjan-oracle/amit-aidp-cicd-tests --ref main`

- [ ] **Step 2: Watch the ephemeral runner**

Run (on box): `kubectl get pods -n arc-runners -w`
Expected: an ephemeral runner pod appears, runs, then terminates.

- [ ] **Step 3: Confirm the run + AIDP state**

- `gh run list --repo amitranjan-oracle/amit-aidp-cicd-tests --workflow aidp-cicd-oke` → latest `success`.
- Via MCP: git folder `cicd_folder/amit-aidp-cicd-tests` pulled; `cicd_01`
  cluster and `cicd_workflow_job` in-sync (idempotent no-op if VM already
  converged them). Report exactly what was created/updated/no-op'd.

---

## Task 14: README (`docs/oke-runner-setup.md`) + finish

**Files:** Create `docs/oke-runner-setup.md`; Modify
`docs/superpowers/plans/2026-06-08-oke-runner.md` (check off)

- [ ] **Step 1: Write the runbook** covering, in order, with the exact commands used:
  0. Overview + relationship to the VM runner (coexist) + cost note (delete/scale-to-0).
  1. Prerequisites: OCI CLI auth, Enhanced-cluster requirement, tooling on amitdemografana.
  2. Network/security: VCN/subnet reuse, the two NSGs + full rule table (from Task 5), flannel rationale, private endpoint + kubectl-via-amitdemografana.
  3. Provision: `provision-cluster.sh` (what it creates, how to poll, how to delete).
  4. Workload identity: `create-workload-dg.sh`, the matching rule, and the **user** role-add to `AI_DATA_PLATFORM_ADMIN` (GROUP member).
  5. ARC bootstrap: `bootstrap-runner.sh`, PAT-from-Vault (never echoed), the
     `runs-on: amit-cicd-oke` == release-name fact, verify runner online.
  6. AIDP git credential: `init-aidp-credential.sh` (the ConfigMap-mount form),
     why it must run under WI (ownership), resolve-by-name in `aidp_deploy.py`.
  7. Trigger: `cicd-oke.yml` dispatch, watch ephemeral pod, verify via MCP.
  8. Teardown / cost control: scale node pool to 0 or `oci ce cluster delete`.
  9. As-built record (filled after Tasks 9–13): real cluster/DG/NSG OCIDs,
     k8s version, what was installed on amitdemografana, any rule corrections.

- [ ] **Step 2: Commit**

```bash
git add docs/oke-runner-setup.md docs/superpowers/plans/2026-06-08-oke-runner.md
git commit -m "docs(oke): runner setup runbook + as-built"
```

- [ ] **Step 3: Push (SSH) + PR**

```bash
git push -u origin feat/oke-runner          # SSH remote (workflow-scope safe)
gh pr create --repo amitranjan-oracle/amit-aidp-cicd-tests --base main \
  --head feat/oke-runner --title "OKE-hosted GitHub runner for AIDP CI/CD" \
  --body "Adds a private OKE Enhanced cluster + ARC runner using Workload Identity, coexisting with the VM runner. See docs/oke-runner-setup.md."
```

- [ ] **Step 4: Final review** — dispatch a code-reviewer over the whole branch
  (scripts, manifests, workflow, `aidp_deploy.py` diff) before requesting merge.

---

## Notes / known risks (carried from spec §9)
- **DG matching-rule keys** (`resource.k8s.*`) — validated at create; iterate on rejection (Task 6 note).
- **PAT scope** — must be `repo`; if ARC registration fails on scope, surface it.
- **WI signer** — needs Enhanced cluster + projected SA token; if it can't bootstrap in-pod, capture the error (don't assume "no privilege").
- **Node-join failures** almost always = NSG port gaps (6443/12250/10250) — check first.
- **`init-aidp-credential.sh`** — the driver (`mkcred.py`) is mounted via a ConfigMap into a WI pod; the PAT reaches the pod from the `aidp-cicd-pat` k8s Secret as env (never echoed). Requires Task 10 role-add done first, else the AIDP POST 401/403s.

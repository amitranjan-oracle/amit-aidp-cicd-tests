# Scriptable AIDP Bundle Deployment (Python + Resource Principal)

How to drive an **AIDP bundle deploy** and a **git pull** from Python, signing requests
with the **OCI resource-principal signer**.

Sources studied:
- `~/IdeaProjects/datahub/` — `datahub-dp/bundles-service`, `datahub-dp/git-service`,
  `datahub-dp/api-handler` (API specs + Java service/CLI implementation).
- `~/IdeaProjects/oci-python-sdk/` — the OCI Python SDK (signer + generated AIDP client).

Companion script: **`aidp_deploy.py`** (in this directory). It compiles clean
(`python3 -m py_compile aidp_deploy.py`) and uses only `oci` + `requests`.

---

## 1. Key finding: there is NO data-plane SDK client

The generated public client `oci.ai_data_platform.AiDataPlatformClient`
(`oci-python-sdk/src/oci/ai_data_platform/`, `API Version: 20240831`) only manages the
**control-plane resource** — verified operations:

```
cancel_work_request, change_ai_data_platform_compartment, create_ai_data_platform,
delete_ai_data_platform, get_ai_data_platform, get_work_request, list_ai_data_platforms,
list_work_request_errors, list_work_request_logs, list_work_requests, update_ai_data_platform
```

It has **no** bundle, git, or workspace operations. **Bundle-deploy and git-pull are
data-plane / workspace operations** exposed by the datahub-dp service and are not in the
generated SDK. → We call them as **raw HTTPS requests signed with the OCI signer**.

### The signer is a `requests` auth object

`oci.auth.signers.get_resource_principals_signer()`
(`oci-python-sdk/src/oci/auth/signers/resource_principals_signer.py:57`) returns a signer
whose MRO is:

```
EphemeralResourcePrincipalSigner -> SecurityTokenSigner -> AbstractBaseSigner -> AuthBase -> object
```

`oci.signer.AbstractBaseSigner(requests.auth.AuthBase)` implements
`__call__(self, request, enforce_content_headers=True)` (`signer.py:175,215`), so it plugs
directly into `requests`:

```python
requests.request(method, url, data=body_bytes, auth=signer, ...)
```

For body-bearing requests the signer auto-adds `x-content-sha256`, `content-type`,
`content-length`. This is the exact pattern datahub itself uses
(`datahub/integration-testing-tools/commands/oci_tools.py` → `requests.request(..., auth=self._signer)`).

---

## 2. Resource-principal signer construction

```python
import oci
signer = oci.auth.signers.get_resource_principals_signer()
```

Works when the caller runs **inside OCI** (an AIDP job / function / instance) with the
RPST environment injected. Required env vars
(`resource_principals_signer.py:16-20`):

| Env var | Meaning |
|---|---|
| `OCI_RESOURCE_PRINCIPAL_VERSION` | Selects the RP flavor (e.g. `2.2`, `3.0`); presence = "use RP" |
| `OCI_RESOURCE_PRINCIPAL_RPST` | Resource Principal Session Token |
| `OCI_RESOURCE_PRINCIPAL_PRIVATE_PEM` | Private key (PEM) |
| `OCI_RESOURCE_PRINCIPAL_PRIVATE_PEM_PASSPHRASE` | Key passphrase (optional) |
| `OCI_RESOURCE_PRINCIPAL_REGION` | Canonical region name |

Local/dev alternatives (same `auth=` contract):
`oci.auth.signers.InstancePrincipalsSecurityTokenSigner()`, or a config-file
`SecurityTokenSigner` (`oci.config.from_file` + `SecurityTokenSigner`).

> The resource principal's IAM policy must grant the deploy/git permissions
> (`AI_DATA_PLATFORM_UPDATE` / `AI_DATA_PLATFORM_READ`, or the `DATA_LAKE_*` equivalents).

---

## 3. API contracts (verified from specs)

Public host/base path (from the deploy `x-example`):
`https://aidp.<region>.oci.oraclecloud.com/20240831`. Internal specs are written against
`/dataLakes/{dataLakeId}/...`; the published resource is `/aiDataPlatforms/{aiDataPlatformId}/...`.

> Confirm the exact host for your region/realm with your tenancy — the script uses the
> documented `aidp.<region>.oci.oraclecloud.com` form.

### 3a. Git pull
`datahub-dp/git-service/git-service-spec/src/specs/api.cond.yaml:887` (also api-handler git paths)

```
POST /20240831/aiDataPlatforms/{aiDataPlatformId}/workspaces/{workspaceKey}/gitRepositories/{gitRepositoryKey}/actions/pull
operationId: GitPull
```

- **Body** `GitPullDetails`: `{ "gitFolderName": "<folder for the branch>" }`
  (the api-handler/UI variant also accepts `gitFolderPath`, `branchName`, `pullAction`).
- **Headers**: `opc-request-id`, `opc-retry-token`, `if-match` (optional).
- **Response**: `202 Accepted`, async. Tracking header `datalake-async-operation-key`
  (a.k.a. `aidp-async-operation-key`).
- Permission: `AI_DATA_PLATFORM_READ` or `DATA_LAKE_READ`.
- Impl: `GitOperationsService.gitPull` → async `GitPullHandler` → CLI executor uses
  **`PULL_WITH_AUTOSTASH`** (`git stash --keep-index -u` → `git merge origin/<branch>` →
  `git stash pop`), then syncs changed files to the workspace volume.

### 3b. Deploy bundle
`datahub-dp/bundles-service/bundles-service-spec/src/specs/api.cond.yaml:327`

```
POST /20240831/aiDataPlatforms/{aiDataPlatformId}/workspaces/{workspaceKey}/bundles/actions/deploy
operationId: DeployBundle   (Preview)
```

- **Body** `DeployBundleDetails` (only `path` required — bundle root folder in the workspace volume):
  ```json
  { "path": "/Workspace/git/demo-team/customer_churn_bundle" }
  ```
- **Response**: `202 Accepted`, async. Headers `opc-request-id`, `datalake-async-operation-key`.
- Legacy alias: `POST .../workspaces/{workspaceKey}/actions/deployBundle` (`DeployBundleWorkspaceAction`).
- Impl: `BundleResource.deployBundle` → `bundleService.deployBundle` (submitted async);
  orchestration in `deploy/orchestration/DeployBundleHandler.java`.

### 3c. Poll — generic async operation (used for git pull)
`datahub-dp/api-handler/datahub-dp-spec/src/specs/internal/paths.cond.yaml:1178`

```
GET /20240831/aiDataPlatforms/{aiDataPlatformId}/asyncOperations/{asyncOperationKey}
operationId: GetAiDataPlatformAsyncOperation   ->  AsyncOperation
```

`AsyncOperation.status` enum (`internal/definitions.cond.yaml:300`):
`IN_PROGRESS | SUCCEEDED | FAILED | CANCELED`.
(`actionType` for a git pull = `GIT_OPERATION_PULL`.)

### 3d. Poll — bundle deployment status (used for deploy)
`bundles-service-spec/src/specs/api.cond.yaml:730`

```
POST /20240831/aiDataPlatforms/{aiDataPlatformId}/workspaces/{workspaceKey}/bundles/actions/getDeploymentStatus
operationId: FetchBundleDeploymentStatus   ->  BundleDeploymentStatus   (HTTP 200)
Body: { "path": "<bundle root path>" }
```

`BundleDeploymentStatus.status` enum:
`SUCCEEDED | FAILED | IN_PROGRESS | NOT_DEPLOYED`; also returns `timeStarted`,
`timeCompleted`, `message`, and `resources[]` (`{type: JOB|AGENTFLOW|CLUSTER|FILE, key, name}`).

Either poll mechanism works for deploy; `getDeploymentStatus` gives the richer
bundle-level summary, so the script uses it for the deploy phase.

---

## 4. End-to-end flow

```
git pull (202) ──▶ poll asyncOperations/{key} until SUCCEEDED
                         │
                         ▼
deploy bundle (202) ──▶ poll getDeploymentStatus until SUCCEEDED / FAILED
```

---

## 5. The script (`aidp_deploy.py`)

Run it with these env vars (plus the `OCI_RESOURCE_PRINCIPAL_*` set provided by the platform):

| Env var | Example |
|---|---|
| `AIDP_REGION` | `us-ashburn-1` (defaults to `OCI_RESOURCE_PRINCIPAL_REGION`) |
| `AIDP_OCID` | `ocid1.aidataplatform.oc1.iad.aaaa...` |
| `AIDP_WORKSPACE_KEY` | `e15a4ac0-bdc9-4f2a-9a48-c34ffa864bd3` |
| `AIDP_GIT_REPO_KEY` | git repository key (optional — skips pull if unset) |
| `AIDP_GIT_FOLDER` | `gitFolderName` to pull (optional) |
| `AIDP_BUNDLE_PATH` | `/Workspace/git/demo-team/customer_churn_bundle` |
| `AIDP_VERIFY_TLS` | `true` (set `false` only for test endpoints) |

```bash
export AIDP_OCID=ocid1.aidataplatform.oc1.iad.aaaa...
export AIDP_WORKSPACE_KEY=e15a4ac0-bdc9-4f2a-9a48-c34ffa864bd3
export AIDP_BUNDLE_PATH=/Workspace/git/demo-team/customer_churn_bundle
export AIDP_GIT_REPO_KEY=...        # optional
export AIDP_GIT_FOLDER=...          # optional
python3 aidp_deploy.py
```

Core of the implementation (full file in `aidp_deploy.py`):

```python
import json, os, time, requests, oci

API_VERSION = "20240831"
signer = oci.auth.signers.get_resource_principals_signer()   # requests.auth.AuthBase

def aidp_root(region, aidp_id):
    return f"https://aidp.{region}.oci.oraclecloud.com/{API_VERSION}/aiDataPlatforms/{aidp_id}"

def signed(method, url, body=None, signer=signer):
    headers = {"accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")          # exact bytes the signer hashes
        headers["content-type"] = "application/json"
    return requests.request(method, url, data=data, headers=headers, auth=signer, timeout=60)

# 1) git pull -> 202 + datalake-async-operation-key
r = signed("POST",
    f"{aidp_root(region, aidp_id)}/workspaces/{ws}/gitRepositories/{repo}/actions/pull",
    {"gitFolderName": folder})
pull_key = r.headers["datalake-async-operation-key"]

# 2) poll asyncOperations/{key} until status == SUCCEEDED
while True:
    op = signed("GET", f"{aidp_root(region, aidp_id)}/asyncOperations/{pull_key}").json()
    if op["status"] == "SUCCEEDED": break
    if op["status"] in ("FAILED", "CANCELED"): raise RuntimeError(op)
    time.sleep(5)

# 3) deploy bundle -> 202
signed("POST",
    f"{aidp_root(region, aidp_id)}/workspaces/{ws}/bundles/actions/deploy",
    {"path": bundle_path})

# 4) poll getDeploymentStatus until SUCCEEDED / FAILED
while True:
    st = signed("POST",
        f"{aidp_root(region, aidp_id)}/workspaces/{ws}/bundles/actions/getDeploymentStatus",
        {"path": bundle_path}).json()
    if st["status"] == "SUCCEEDED": break
    if st["status"] == "FAILED": raise RuntimeError(st["message"])
    time.sleep(5)
```

---

## 6. Caveats / things to verify in your environment

- **Endpoint host**: the script uses `aidp.<region>.oci.oraclecloud.com` (from the spec's
  documented `x-example`). Confirm the real host/realm for your tenancy; in some realms the
  hostname or base path differs.
- **Deploy is `(Preview)`** in the spec — surface and behavior may still change.
- **Body signing**: pass the same bytes you send (the script uses `data=json.dumps(...).encode()`,
  not `json=`), so `x-content-sha256` matches the body.
- **IAM policy**: the resource principal needs deploy/git permissions on the target AIDP/workspace.
- **TLS**: keep `verify=True` against real endpoints. (`oci_tools.py` uses `verify=False`
  for internal test hosts only.)
- **Bundle layout** the deploy reads (reference fixture
  `datahub-dp/bundles-service/.../test/resources/sample_bundle/`):
  `aidp_workbench.yaml` (manifest) · `jobs/*.job.json` + `jobs/dependencies/*.compute.json`
  (workflows + compute) · `agentflows/*.aflow.json` + `agentflows/dependencies/*.aicompute.json`
  · `artifacts/**` (notebooks/scripts) · `.aidp/` (state/origins/overrides).

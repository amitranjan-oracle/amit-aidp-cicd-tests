# AIDP CI/CD on push to `main` — Design Spec

**Date:** 2026-06-07
**Repo:** `amitranjan-oracle/amit-aidp-cicd-tests` (public)
**Status:** Approved design — pending final spec review before implementation plan.

## 1. Goal

On every push to `main`, a GitHub Actions workflow runs a self-contained Python script **on the OCI compute `amit-cicd-compute`** that, using the box's instance principal, brings AIDP to the desired state captured in committed JSON spec files:

1. Ensures `/Workspace/cicd_folder` exists, then **creates** (clones) the AIDP git folder for this repo's `main` branch under it — or **pulls** `main` if it already exists.
2. **Reconciles the compute** `ephemeral_01` (a Spark cluster) from `specs/ephemeral_01.cluster.json`: create if absent, update if the live config differs, no-op if identical.
3. **Reconciles the workflow job** `cicd_workflow_job` from `specs/cicd_workflow_job.job.json` (cloned from `7fa6a6bf…`, repointed to `ephemeral_01`): create / update / no-op, same way.

All identifiers/config come from a YAML config file committed to the repo. The two resource definitions are committed JSON "desired state".

## 2. Environment (verified live)

**Execution box — `amit-cicd-compute` (private subnet, the CI/CD box):**
- Private IP `10.0.1.84`; reached for setup via the public box as a bastion: `ssh -J opc@144.25.95.237 opc@10.0.1.84`. Same keypair (`ssh-key-2025-08-29.key`) authorizes `opc` on both.
- **Outbound internet: yes** (`github.com`/`pypi` → 200) → a GitHub **self-hosted runner** registers & long-polls GitHub with **no inbound exposure**.
- Reaches AIDP (OCI). **Python 3.9.25** with `oci 2.170.0`, `requests 2.27.1`, `PyYAML 5.4.1` pre-installed.
- Oracle Linux 9.7, x86_64. **Compartment = DataServices** (`…b5v5zva`) → matched by `DataServices-Compute-DG`; instance-principal auth to `ai-data-platform-family` (IAM policy must be in place). No runner installed yet.

**Not in the runtime path:** `amitdemografana` (public `144.25.95.237`, no egress, Python 3.6.8) — original target; now only the SSH bastion to reach the private box.

## 3. Decisions (locked)

| Decision | Choice |
|---|---|
| Trigger | GitHub **self-hosted runner on `amit-cicd-compute`** (`runs-on: [self-hosted, aidp]`); outbound-only (see §5). |
| Execution | Runner runs steps locally; Python 3.9; instance principal. |
| Desired state | Two committed JSON files under `specs/` (cluster + job). The script applies them declaratively. |
| Reconcile model | For each resource: resolve by **name** → **create** if absent → **update** if normalized config differs → **no-op** if identical. |
| Order | **compute (`ephemeral_01`) before job (`cicd_workflow_job`)** — the job references the cluster. |
| Job ↔ cluster link | Job references the cluster **by name**; the script resolves `ephemeral_01`'s live key post-compute-phase and injects `clusterKey` into the job body. (A recreated cluster gets a new key, so the key is never hard-coded in the spec.) |
| Auth signer | Auto-detect: RP env → else `InstancePrincipalsSecurityTokenSigner`. |
| Config | YAML at `config/cicd.yaml`; identifiers only; `git_credential_key` is a reference, never the PAT. |
| Script style | Self-contained; `oci`+`requests`+`yaml`+stdlib; raw signed HTTPS; payloads validated against the `aidp_agent` client. |
| `cicd_workflow_job` tasks | Cloned from `7fa6a6bf…` (2 sequential notebook tasks), **name → `cicd_workflow_job`**, **cluster → `ephemeral_01`**; tasks/sequence/params otherwise identical. |

## 4. Verified AIDP facts

- **Template job `7fa6a6bf-…`** (`access_parameter.job`, workspace `playground`): 2 sequential `NOTEBOOK_TASK`s `set_parameter` → `print_parameter` (`dependsOn`, `runIf: ALL_SUCCESS`) on `/Workspace/Shared/default_examples/*.ipynb`; cluster currently `small_cluster_16gb` (**FAILED**) → repointed to `ephemeral_01`; job param `job_param=custom_value`.
- **`ephemeral_01`** (cluster key `a3ad61d1-b63b-4eb2-98fe-5a0e0f3fe815`, ACTIVE, type `USER`): driver `amd.generic` 2 ocpu/32 GB; workers `amd.generic` 2 ocpu/32 GB, min=max=1; Spark 3.5.0 with `spark.{executor,driver}.extraJavaOptions=-Dcom.amazonaws.services.s3.enableV4=true`. (Currently has 25 attached sessions — see §8 caveat on updating an active cluster.)
- **Endpoint** (mirrors live `base_client.py`): `https://aidp.{region}.oci.oraclecloud.com/{api_version}/{path_prefix}/{data_lake_id}/workspaces/{ws}/...`; working pair `dataLakes`+`20240831`+data-lake OCID.
- **OCI signer** is `requests.auth.AuthBase`; send exact body bytes via `data=json.dumps(body).encode()`.
- **`update_job` is full-replace PUT** → GET current, PUT merged. **Git pull async** → poll `asyncOperations/{key}`.

## 5. Architecture

```
 git push → main → .github/workflows/cicd.yml (on: push:[main] + workflow_dispatch, runs-on: [self-hosted, aidp])
   ── job delivered down the runner's OUTBOUND long-poll to GitHub ──
   on amit-cicd-compute:
   1 checkout  →  2 preflight import oci,requests,yaml  →  3 python3 aidp_cicd.py --config config/cicd.yaml
        │  signer → InstancePrincipalsSecurityTokenSigner (DataServices)
        ├─ Phase 1  ensure /Workspace/cicd_folder
        ├─ Phase 2  gitFolder CREATE (clone) ── or ── PULL main → poll async
        ├─ Phase 3  reconcile COMPUTE ephemeral_01  ← specs/ephemeral_01.cluster.json   (create | update | no-op)
        └─ Phase 4  reconcile JOB cicd_workflow_job ← specs/cicd_workflow_job.job.json   (create | update | no-op)
                       (resolve ephemeral_01 live key → inject into jobClusters + task cluster refs)
```

**How a private box gets triggered (outbound-only):** the runner opens an **outbound** HTTPS long-poll to GitHub and waits ("Listening for Jobs"); on push, GitHub pushes the queued job **down that existing outbound connection**. GitHub never initiates an inbound connection, so no public IP / inbound `:22` / SSH-from-GitHub is needed — only egress (NAT), which `amit-cicd-compute` has.

## 6. Components (files added to `amit-aidp-cicd-tests`)

| File | Responsibility |
|---|---|
| `.github/workflows/cicd.yml` | `on: push:[main]` + `workflow_dispatch`; `runs-on: [self-hosted, aidp]`; checkout → dep preflight → `python3 aidp_cicd.py`. No secrets. |
| `aidp_cicd.py` | Python 3.9 orchestrator + thin AIDP data-plane client; Phases 1–4; generic reconcile helper. Flags `--config`, `--dry-run`. |
| `config/cicd.yaml` | Identifiers + spec-file paths + `git_credential_key`. |
| `specs/ephemeral_01.cluster.json` | Desired-state cluster definition (see §10). |
| `specs/cicd_workflow_job.job.json` | Desired-state job definition (cloned from `7fa6a6bf…`, name + cluster repointed; cluster by name). |
| `requirements-cicd.txt` | `oci`,`requests`,`pyyaml` — documentation/pin (deps already on the box; used by preflight). |
| `docs/self-hosted-runner-setup.md` | One-time runner install on `amit-cicd-compute` + principal sanity check + public-repo safety notes. |

## 7. Reconcile behavior

Generic per-resource reconcile (used for both cluster and job):

```
desired = load(spec_json)                       # managed fields only
live    = resolve_by_name(desired.name)         # list + match displayName/name
if live is None:                                # CREATE
    create(desired);  log "created <name>"
else:
    if normalize(live) == normalize(desired):   # NO-OP
        log "<name> already in sync"
    else:                                        # UPDATE
        log diff(normalize(live), normalize(desired))
        update(live.key, merged(live, desired))
```

- **Normalization** compares only **managed fields** (allowlists in §9); server-managed/volatile fields are stripped on both sides so steady state is a no-op.
- **Phase 1 — dir:** create `/Workspace/cicd_folder`; treat already-exists/`409` as success.
- **Phase 2 — git folder:** match on `folder_path`; not found → `POST gitFolders` (clone); found → `POST gitRepositories/{key}/actions/pull {pullAction:"PULL"}` → poll.
- **Phase 3 — compute `ephemeral_01`:** `GET clusters` (match `displayName`); create `POST clusters` / update `PUT clusters/{key}` per the generic flow.
- **Phase 4 — job `cicd_workflow_job`:** resolve `ephemeral_01` key (must exist after Phase 3) → set `jobClusters[].clusterKey` and each `tasks[].cluster.clusterKey` → reconcile (`POST jobs` / GET-then-`PUT jobs/{key}`). Job diff compares cluster refs **by name** (ignore the volatile key).

## 8. Error handling & caveats

- No principal → fail fast. Non-2xx → raise with status + body + `opc-request-id`. Missing dep → preflight fails loudly.
- Async ops → bounded poll (`poll_timeout_secs`); raise on `FAILED`/`CANCELED`; `TimeoutError` past deadline.
- `update_job` → GET-then-merge (replace-PUT safe).
- **Updating an ACTIVE cluster:** `ephemeral_01` is ACTIVE with live sessions; a cluster update may be disruptive or require a stop/restart, and some shape fields may be immutable while running. The spec is captured **from the live cluster**, so steady-state reconcile is a **no-op** (no disruptive update unless the JSON is intentionally changed). The implementation will confirm cluster-update semantics (which fields are mutable in-place) and, if an update is needed on an active cluster, surface it clearly rather than silently restarting. `--dry-run` shows the intended action first.

## 9. Managed-field allowlists (for normalization/diff)

- **Cluster:** `displayName`, `description`, `type`, `nodeType`, `driverConfig`, `workerConfig`, `clusterRuntimeConfig`, `autoTerminationMinutes`, `loggingConfig`.
  Excluded: `key`, `state`, `stateDetails`, `activeClusterResources`, `jdbcEndpointUrl`, `logId`, `logGroupId`, `attachedNotebooks`, `attachedSessions`, `sessions`, `sourceApi`, `attachedAgentFlowCount`, `subscription`, `stoppedBy*`, `timeCreated`, `timeUpdated`, `createdBy*`, `updatedBy*`.
- **Job:** `name`, `description`, `path`, `tasks`, `jobClusters`, `parameters`, `schedule`, `maxConcurrentRuns`, `timeoutSeconds`, `continuous`, `queue`, `jobTag`, `runAs`, `gitConfig`. Cluster refs compared by `clusterName` (ignore `clusterKey`).
  Excluded: `key`, `timeCreated`, `timeUpdated`, `createdBy*`, `updatedBy*`.

## 10. Config schema (`config/cicd.yaml`)

```yaml
aidp:
  region: us-ashburn-1
  data_lake_ocid: ocid1.datalake.oc1.iad.amaaaaaaai22xpqarb4qw6bcev7yokxk3ftd4ucefw2ofs7fbfudefs6x5sa
  path_prefix: dataLakes
  api_version: "20240831"
  workspace_key: f95a83f8-9bd1-4259-a45f-ea1c3a5a7516   # playground

git:
  repository_url: https://github.com/amitranjan-oracle/amit-aidp-cicd-tests.git
  branch: main
  credential_key: "79476e30-b34c-4a1a-8fc5-9a202587f117"   # GIT_ACCOUNT "amitranjan-oracle" (PAT) — reference only
  parent_dir: /Workspace/cicd_folder
  folder_path: /Workspace/cicd_folder/amit-aidp-cicd-tests

compute:
  name: ephemeral_01
  spec_file: specs/ephemeral_01.cluster.json

workflow:
  name: cicd_workflow_job
  spec_file: specs/cicd_workflow_job.job.json
  cluster_name: ephemeral_01          # resolved to key at deploy time

options:
  verify_tls: true
  poll_timeout_secs: 600
  poll_interval_secs: 5
```

### `specs/ephemeral_01.cluster.json` (captured desired state)

```json
{
  "displayName": "ephemeral_01",
  "description": null,
  "type": "USER",
  "nodeType": null,
  "driverConfig": {
    "driverShape": "amd.generic",
    "driverShapeConfig": { "ocpus": 2, "gpus": 0, "memoryInGBs": 32 }
  },
  "workerConfig": {
    "workerShape": "amd.generic",
    "workerShapeConfig": { "ocpus": 2, "gpus": 0, "memoryInGBs": 32 },
    "minWorkerCount": 1,
    "maxWorkerCount": 1
  },
  "clusterRuntimeConfig": {
    "type": "SPARK",
    "initScripts": [],
    "sparkVersion": "3.5.0",
    "sparkAdvancedConfigurations": {
      "spark.executor.extraJavaOptions": "-Dcom.amazonaws.services.s3.enableV4=true",
      "spark.driver.extraJavaOptions": "-Dcom.amazonaws.services.s3.enableV4=true"
    },
    "sparkEnvVariables": {}
  },
  "autoTerminationMinutes": null,
  "loggingConfig": null
}
```

### `specs/cicd_workflow_job.job.json` (from `7fa6a6bf…`, name + cluster repointed)

```json
{
  "name": "cicd_workflow_job",
  "description": "",
  "path": "jobs",
  "maxConcurrentRuns": 1,
  "parameters": [ { "name": "job_param", "value": "custom_value" } ],
  "jobClusters": [ { "clusterName": "ephemeral_01", "clusterKey": null, "newCluster": null } ],
  "tasks": [
    { "type": "NOTEBOOK_TASK", "taskKey": "set_parameter", "dependsOn": [],
      "runIf": "ALL_SUCCESS", "maxRetries": 0,
      "notebookPath": "/Workspace/Shared/default_examples/set_parameter.ipynb",
      "cluster": { "clusterName": "ephemeral_01", "clusterKey": null, "newCluster": null },
      "parameters": [] },
    { "type": "NOTEBOOK_TASK", "taskKey": "print_parameter",
      "dependsOn": [ { "taskKey": "set_parameter", "outcome": null } ],
      "runIf": "ALL_SUCCESS", "maxRetries": 0,
      "notebookPath": "/Workspace/Shared/default_examples/print_parameter.ipynb",
      "cluster": { "clusterName": "ephemeral_01", "clusterKey": null, "newCluster": null },
      "parameters": [
        { "name": "job_param",  "value": "{{job.parameters.job_param}}" },
        { "name": "task_param", "value": "{{job.parameters.job_param}}" }
      ] }
  ]
}
```
`clusterKey: null` is filled at deploy time from the live `ephemeral_01` (Phase 3 guarantees it exists first).

## 11. Python compatibility (`aidp_cicd.py`)

Target **Python 3.9** (box interpreter). f-strings/`typing`/`dataclasses` fine; avoid 3.10+-only syntax (`match`). Only `oci`/`requests`/`yaml` + stdlib.

## 12. Testing / verification

- **Static:** `python3 -m py_compile aidp_cicd.py`.
- **Dry run:** `aidp_cicd.py --config … --dry-run` — load+validate config & spec files, select+log signer, log the create/update/no-op decision + diff per resource; mutate nothing. Runnable off-box.
- **Live (per project rule):** `workflow_dispatch` → runner picks up job → verify via AIDP MCP: `list_files /Workspace/cicd_folder/amit-aidp-cicd-tests`; `ephemeral_01` matches spec; `cicd_workflow_job` has 2 sequential tasks on `ephemeral_01`. Re-run to prove idempotency: pull + **both reconciles report no-op**, no dupes.

## 13. Self-hosted runner setup (`docs/self-hosted-runner-setup.md`)

1. Reach the box via bastion (`ssh -J opc@144.25.95.237 opc@10.0.1.84`).
2. Principal sanity check: build `InstancePrincipalsSecurityTokenSigner()` + a read-only AIDP `GET`.
3. Install runner (linux-x64): `./config.sh --url https://github.com/amitranjan-oracle/amit-aidp-cicd-tests --token <REG_TOKEN> --labels self-hosted,aidp --unattended`.
4. Service: `sudo ./svc.sh install opc && sudo ./svc.sh start` → "Listening for Jobs".
5. Public-repo safety: trigger only on `push`/`workflow_dispatch` on `main` (never `pull_request` from forks); least-privilege IAM on the box's principal.

## 14. Open items to confirm at spec review

1. Workspace = `playground` (`f95a83f8…`). ✅?
2. Git folder path = `/Workspace/cicd_folder/amit-aidp-cicd-tests`; spec files under `specs/`. ✅?
3. Runner labels `self-hosted, aidp` + service install. ✅?

## 15. Out of scope (YAGNI)

- Repointing job tasks to notebooks inside the pulled git folder (repo has no notebooks yet).
- Bundle deploy (existing `aidp_deploy.py`).
- Any `amitdemografana`/Python 3.6.8 runtime path.
- Managing resources beyond `ephemeral_01` + `cicd_workflow_job`; scheduling; notifications.
```

# Workflow `runAs`: service-account credential for scheduled execution

## Why this is needed

The bundle is deployed by an **instance principal** (the self-hosted runner). When a
scheduled workflow run later starts, the workflow engine does **not** re-use the live
deploy-time auth — it reconstructs the execution principal from the job's stored
`createdBy`. For an instance-principal-created job that value is the **instance OCID**,
which is not an IAM user, so the runtime `GetCluster` RBAC check fails:

```
WORKFLOW_EXECUTION_0055 … GetCluster … (404, NotAuthorizedOrNotFound) RBAC check failed
for RBAC Permission Type: READ …
```

(Root-cause detail and the create-time-vs-run-time split are in the Engg ticket under
`/Users/amitranjan/OracleContent/idl/issues/workflow_created_from_instance_principal_fails_in_rbac`.)

The fix is to set the job's **`runAs`** to a **Credential Store `SERVICE_ACCOUNT`
credential key**. At run time the engine then executes as that credential's `userId`
(a real IAM user that is a member of `AI_DATA_PLATFORM_ADMIN`), so the RBAC check passes.

How the engine interprets `runAs` (datahub `JobPrincipalFetcher.getResolver`):
- **empty/null** → legacy fallback = reconstruct from `createdBy` (the broken path).
- **`"YOURSELF"`** → resolve from the invoker's IAM-user-credential setting / delegation
  token. For a scheduled, instance-principal-created job this falls back to the instance
  OCID again, so it does **not** help here.
- **any other value** → treated as a **credential key** → `ServiceAccountPrincipalResolver`
  resolves that credential and builds the principal as its `userId`.

## What a `SERVICE_ACCOUNT` credential is

It is a stored **OCI API key for an IAM user** — `ServiceAccountCredentialDetails`:
`userId`, `tenancy`, `region`, `fingerprint`, encrypted `privateKey`, `isReadOnly`.

## 1. Register the credential for the `runAs` key

API (verified against the HAR at
`~/OracleContent/idl/api_exploration/credential_store/05-service-account.har`):

```
POST https://aidp.{region}.oci.oraclecloud.com/20240831/dataLakes/{dataLakeId}/credentialsV2
{
  "displayName": "<name>",
  "type": "SERVICE_ACCOUNT",
  "credentialDetails": {
    "credentialType": "SERVICE_ACCOUNT",
    "userId":      "<target IAM user OCID>",
    "fingerprint": "<target API key fingerprint>",
    "tenancy":     "<tenancy OCID>",
    "region":      "<region>",
    "isReadOnly":  false,
    "privateKey":  "<target user's API private key (PEM)>"
  }
}
```

> The create returns **HTTP 200 with an empty body** — read the key back via a list (below).
> **Who must make this call matters — see [Limitations](#limitations).** It must be a
> *different* IAM admin user than the target, and **not** an instance principal.

Reference snippet (run locally; signs as a *second* admin profile, registers the target
profile's key — never echo the private key):

```python
import json, oci, requests
DL   = "<dataLakeId>"
BASE = f"https://aidp.us-ashburn-1.oci.oraclecloud.com/20240831/dataLakes/{DL}/credentialsV2"
tgt  = oci.config.from_file(profile_name="DEFAULT")          # target user (whose key we store)
adm  = oci.config.from_file(profile_name="ACCESSVERIFIER")   # a DIFFERENT admin user = caller
signer = oci.signer.Signer(adm["tenancy"], adm["user"], adm["fingerprint"],
                           adm["key_file"], pass_phrase=adm.get("pass_phrase"))
body = {"displayName": "amit_ranjan_user_account", "type": "SERVICE_ACCOUNT",
        "credentialDetails": {"credentialType": "SERVICE_ACCOUNT",
            "userId": tgt["user"], "fingerprint": tgt["fingerprint"],
            "tenancy": tgt["tenancy"], "region": tgt["region"],
            "isReadOnly": False, "privateKey": open(tgt["key_file"]).read()}}
r = requests.post(BASE, data=json.dumps(body).encode(),
                  headers={"content-type": "application/json"}, auth=signer)
print(r.status_code)   # 200, empty body
```

The MCP equivalent (`aidp_credentials action=create`) also works, **provided the MCP is
authenticated as a non-target admin user** (same limitations apply).

## 2. Retrieve the `runAs` key and wire it into the bundle

`runAs` is the credential **key (UUID), not the display name** — the resolver looks the
credential up strictly by key (`CredentialStoreConnector.getDataLakeCredential` →
`.credentialV2Key(runAs)`, a GET-by-key). A name will **not** resolve and fails at run
time with `WORKFLOW_EXECUTION_0036`.

Get the key by listing and matching on `displayName`:

```python
r = requests.get(BASE, headers={"accept": "application/json"}, auth=signer)
print({c["displayName"]: c["key"] for c in r.json()["items"]})
# -> {'amit_ranjan_user_account': '1244eed9-2e98-4b0f-a2e5-53f6a2f51e00', ...}
```

(Or `aidp_credentials action=list`.) Then set it in the bundle job spec:

```jsonc
// bundle/jobs/cicd_workflow_job.job.json
"runAs": "1244eed9-2e98-4b0f-a2e5-53f6a2f51e00",
```

Push to `main`; the CICD bundle deploy applies it. **Why the bundle (not a manual job
update):** the job `create` path ignores `runAs` (only the `update` path persists it),
and a job update requires **per-job `ADMIN`**, which the bundle deploy has (it runs as the
instance principal that owns the job). Keeping `runAs` in the bundle also makes it durable
— it is a bundle-reconciled field (`DriftComparator` includes `/runAs`), so a manual edit
would otherwise be reverted on the next deploy.

Verify after deploy: `aidp_jobs action=get_job` → `runAs` shows the key.

## Limitations

- **No self-registration.** A user cannot create a `SERVICE_ACCOUNT` credential whose
  `userId` is **their own** — `CredentialV2Resource.preventExternalSelfIssuedServiceAccount`
  rejects it: *"SERVICE_ACCOUNT cannot be created from the logged-in user."* (HTTP 403.)
- **No instance-principal registration.** A resource/instance principal cannot create one
  either: the handler grants the new credential to the caller's `userId` **before** storing
  it, and `fetchUserId()` is `null` for a non-USER principal →
  `CredentialV2Resource.java:164` throws *"Failed to grant permissions: userId resolved to
  null for caller"* (HTTP 500), and the credential is rolled back.
  → **Therefore registration must be done by a *different* IAM admin user** (caller ≠ target,
  and caller is a real user, not a resource principal). That admin must be a member of
  `AI_DATA_PLATFORM_ADMIN`. (In this repo, user **Access Verifier** / profile `ACCESSVERIFIER`
  was used to register the credential for `amit.z.ranjan@oracle.com`.)
- **`runAs` is key-only**, not name (see §2). A readability nicety would be to also accept the
  display name — flagged as an enhancement to Engg.
- **`runAs` can't be set on job create** — only via update / the bundle deploy.
- **Runtime credential read.** The credential is read as the run's invoker — the triggering
  user for a manual run, the job **creator (instance principal)** for a scheduled run. The
  instance principal can read the credential (verified: GET-by-key → 200), so scheduled runs
  resolve it.
- **The stored API key is a real secret with rotation implications.** If the target user's
  API key is rotated/removed, update or re-create the credential.

## MCP / SDK notes

- **`aidp_jobs update_job` has no `run_as` parameter** — it round-trips the full job, so it
  *preserves* an existing `runAs` (won't wipe the value set via the bundle) but can't *set*
  one. The underlying SDK client (`clients/workflow.py update_job(**kwargs)`) does support it.
  Suggested enhancement: expose an optional `run_as` on the MCP `update_job` tool.
- **`aidp_credentials create` supports `SERVICE_ACCOUNT`** (`clients/credentials.py`,
  `CREDENTIAL_TYPES`) and handles the empty-200 response — no change needed; only the
  non-target-admin-caller requirement above applies.

## As-built (this repo, 2026-06-16)

| Item | Value |
|---|---|
| Credential | `amit_ranjan_user_account` |
| Credential key (`runAs`) | `1244eed9-2e98-4b0f-a2e5-53f6a2f51e00` |
| Target user | `amit.z.ranjan@oracle.com` (`DEFAULT` profile) |
| Registered by | user **Access Verifier** (`ACCESSVERIFIER` profile, in `AI_DATA_PLATFORM_ADMIN`) |
| Wired in | `bundle/jobs/cicd_workflow_job.job.json` → `runAs` |
| Validation | manual run `ebb7b330…` → **SUCCESS** (cluster started, both notebooks ran) |

# AIDP RBAC: OKE Workload Identity principals authorized inconsistently per operation

**Status:** open · **Reporter:** amitranjan · **Date:** 2026-06-08
**Component:** AIDP data-plane API handler (`datahub-dp-api`) + Lake authorization
**Severity:** medium — blocks using OKE Workload Identity as the CI/CD runner identity

---

## Summary

An OCI **dynamic group of resource type `workload`** (OKE Workload Identity),
added as a GROUP member of the `AI_DATA_PLATFORM_ADMIN` data-lake role, is
authorized **inconsistently per operation** by the AIDP data-plane REST API for a
workspace. CREATE-class operations (create cluster, create job) succeed, but
LIST-class operations and all workspace-volume / git operations are denied with
RBAC errors — even though the workload principal is unambiguously a member of the
same admin role.

The **identical role and call path works fully for an OCI _instance_ principal**
(a compute-instance dynamic group). So the gap is specific to **workload /
resource principals** on a subset of operations, not a misconfiguration of the
role, dynamic group, or signer (all verified — see [Verification](#verification-done-on-our-side)).

---

## Environment (as-built)

| Item | Value |
|---|---|
| Region / host | `us-ashburn-1` / `aidp.us-ashburn-1.oci.oraclecloud.com` |
| API surface | `/20240831/dataLakes/{dataLakeId}` |
| Data lake OCID | `ocid1.datalake.oc1.iad.amaaaaaaai22xpqarb4qw6bcev7yokxk3ftd4ucefw2ofs7fbfudefs6x5sa` |
| Workspace key | `f95a83f8-9bd1-4259-a45f-ea1c3a5a7516` (playground) |
| Compartment | DataServices `ocid1.compartment.oc1..…sk4x2hgb5v5zva` |
| OKE cluster | `ocid1.cluster.oc1.iad.aaaaaaaaheuhoa3ik57tqef2dapleyotegmbcenb2pplwkzukchuqwarlo4q` |
| Workload DG (in OracleIdentityCloudService) | `ocid1.dynamicgroup.oc1..aaaaaaaa6i75hgee2dk3tly2yjrf2ayksibtnjuulm4oxyhonimvf2g5gilq` |
| DG matching rule | `ALL {resource.type='workload', resource.compartment.id='…sk4x2hgb5v5zva', resource.k8s.cluster.id='…rlo4q', resource.k8s.namespace.name='arc-runners', resource.k8s.serviceaccount.name='aidp-runner-sa'}` |
| Role | `AI_DATA_PLATFORM_ADMIN` (DG OCID added as a GROUP assignee) |
| Pod identity | namespace `arc-runners`, serviceaccount `aidp-runner-sa` |
| SDK signer | `oci.auth.signers.…OkeWorkloadIdentityResourcePrincipalSigner` |

---

## Observed behavior

All calls below are made by the **same** OKE Workload Identity principal, signed
with the WI resource-principal signer, against the same workspace.

| Operation | HTTP call | Result |
|---|---|---|
| Read OCI vault secret | (OCI Secrets) | ✅ allowed |
| Create / list GIT_ACCOUNT user setting | `GET`/`POST .../dataLakes/{dl}/userSettings` | ✅ allowed |
| Read git-folder metadata | `GET .../workspaces/{ws}/gitFolderMetadata` | ✅ allowed |
| **Create cluster** | `POST .../workspaces/{ws}/clusters` | ✅ **allowed** |
| **Create job** | `POST .../workspaces/{ws}/jobs` | ✅ **allowed** |
| **List clusters** | `GET .../workspaces/{ws}/clusters` | ❌ **denied** |
| **List jobs** | `GET .../workspaces/{ws}/jobs` | ❌ **denied** |
| **Git pull** | `POST .../workspaces/{ws}/gitRepositories/{key}/actions/pull` | ❌ **denied** |
| **Make directory** | `POST .../workspaces/{ws}/actions/mkdir` | ❌ **denied** |
| **Create git folder** | `POST .../workspaces/{ws}/gitFolders` (needs mkdir) | ❌ **denied** |

### Error bodies (HTTP 404)

LIST clusters / jobs / git pull:

```json
{"code":"NotAuthorizedOrNotFound",
 "message":"RBAC check failed for RBAC Permission Type: USER with Resource ..."}
```

mkdir / git folder (VolumeRequestHandler):

```json
{"message":"Unknown resource: Requested operation failed due to RBAC check failure Or Not Found VolumeRequestHandler handleRequest isAccessGranted failed for CreateDirectory"}
```

The same nine operations all **succeed** when the caller is the OCI **instance**
principal of a compute instance whose dynamic group is in the same
`AI_DATA_PLATFORM_ADMIN` role (mkdir returns `409 already exists`, lists return
items, git pull/clone succeeds).

---

## Reproduction

**Prereqs:** an OKE cluster (Enhanced) with a pod running under a serviceaccount
that a `workload` dynamic group matches; that DG added as a GROUP member of an
`AI_DATA_PLATFORM_ADMIN` role on a data lake; OCI Python SDK in the pod.

1. From the pod, build the WI signer and confirm the principal type:

   ```python
   import oci
   signer = oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
   # signer is OkeWorkloadIdentityResourcePrincipalSigner
   ```

2. Sign requests with that signer against
   `https://aidp.us-ashburn-1.oci.oraclecloud.com/20240831/dataLakes/{dataLakeId}`.

3. **Expect success** (these pass today):
   - `GET  /userSettings`
   - `GET  /workspaces/{ws}/gitFolderMetadata?folderPath=/Workspace/x`
   - `POST /workspaces/{ws}/clusters`  (body = a cluster spec)
   - `POST /workspaces/{ws}/jobs`      (body = a job spec)

4. **Expect 404 RBAC denial** (the bug):
   - `GET  /workspaces/{ws}/clusters`
   - `GET  /workspaces/{ws}/jobs`
   - `POST /workspaces/{ws}/actions/mkdir`  (body = `{"path":"/Workspace/cicd_folder"}`)
   - `POST /workspaces/{ws}/gitRepositories/{repoKey}/actions/pull`

5. Repeat steps 3–4 from a **compute instance** whose instance-principal DG is in
   the same role → **all succeed**. This isolates the variable to the principal
   *type* (workload vs instance), not the role/DG/network.

A self-contained reconcile that exercises all of these is in this repo:
`deploy/aidp_deploy.py` (phases 0–4). Running it under WI fails at phase 1
(`mkdir`); running it under the instance principal completes green.

---

## Verification done on our side

We ruled out every client-side / IAM cause before filing:

- **DG matching rule** is correct (workload, DataServices compartment, the exact
  cluster id, namespace `arc-runners`, serviceaccount `aidp-runner-sa`). *Note:*
  IDCS GET/LIST omits `matchingRule` unless `attributes=matchingRule` is
  requested — verified with that flag.
- **Role membership:** the workload DG OCID is a GROUP assignee of
  `AI_DATA_PLATFORM_ADMIN` (verified via the AIDP roles API).
- **Signer:** the pod uses `OkeWorkloadIdentityResourcePrincipalSigner` (printed
  `type(signer)` from inside the pod).
- **Pod metadata matches the rule:** the pod's projected SA-token claims are
  `namespace=arc-runners`, `serviceaccount=aidp-runner-sa`; the runner scale-set
  pod template's `serviceAccountName=aidp-runner-sa`.
- **The WI principal IS resolved to the role** — proven because `userSettings`,
  `gitFolderMetadata`, **cluster CREATE**, and **job CREATE** all pass RBAC under
  this exact identity. Only LIST + VolumeRequestHandler ops are denied.
- **Domain-independent:** moving the workload DG from the default domain to
  `OracleIdentityCloudService` did not change the behavior (WI auth still worked;
  mkdir still denied).

⇒ The inconsistency is server-side, specific to how AIDP authorizes
**workload/resource principals** for the denied operations.

---

## Why each operation worked or failed (code study)

> File/line references below are **verified by grep** against
> `IdeaProjects/datahub` (`datahub-dp/api-handler/datahub-dp-api/.../com/oracle/datahub`).
> The decisive role-membership-vs-user-identity distinction itself lives in the
> backend **Lake** authorization service (not in this repo) and is therefore
> **inferred** — marked as such. Paths below are relative to the resources/utils
> packages.

There are **three different reasons** the workload principal is treated
inconsistently — not one. The deciding factor for each operation is the
combination of *(a)* whether the handler converts the principal via
`AuthUtil.getPrimaryPrincipal()` and *(b)* which access-type it then checks.

| Operation | `getPrimaryPrincipal()`? | Access type checked | Result | Mechanism |
|---|---|---|---|---|
| `userSettings` GET/POST | ✅ (via `resolveContext`) `UserSettingsResource.java:338` | service-level (no `WorkspaceAccessType.USER`) | ✅ | light check |
| `gitFolderMetadata` GET | ✅ `GitResource.java:291` | **`FolderAccessType.READ`** `GitResource.java:301` | ✅ | folder-READ check, not USER |
| cluster **CREATE** | ✅ `ClusterResource.java:310` | **`WorkspaceAccessType.PRIVILEGED_USER`** `:325` | ✅ | role-membership check |
| job **CREATE** | ✅ `JobResource.java:155` | privileged/service-level | ✅ | role-membership check |
| cluster **LIST** | ✅ `ClusterResource.java:1202` | **`WorkspaceAccessType.USER`** `:1209` | ❌ | user-identity check |
| job **LIST** | ✅ `JobResource.java:524` | **`USER`** (`JobService` `:557–561`) | ❌ | user-identity check |
| **git pull** | ✅ `GitResource.java:476` | **`WorkspaceAccessType.USER`** `:477` | ❌ | user-identity check |
| **mkdir** / git folder | ❌ **not called** (`WorkspaceObjectResource.mkdir` `:691`; the file's only `getPrimaryPrincipal` is at `:425`, a different method) | Lake volume handler | ❌ | raw resource principal → Lake |

### Mechanism 1 — `PRIVILEGED_USER` (passes) vs `USER` (fails)

`AuthUtil.getPrimaryPrincipal()` (`AuthUtil.java:92–156`) swaps a
`WORKLOAD`/`COMPUTE_CLUSTER` principal for a user principal **only when a
`dh-user-principal` HTTP header is present**:

```java
String dhUserPrincipal = httpHeaders.getHeaderString(DH_USER_PRINCIPAL_HEADER);
if (primaryPrincipal != null
        && primaryPrincipal.getClaimValue(ClaimType.RESOURCE_TYPE).isPresent()
        && (… .contains(WORKLOAD) || … .contains(COMPUTE_CLUSTER))
        && !StringUtils.isBlank(dhUserPrincipal)) {
    primaryPrincipal = getPrincipalFromBase64(dhUserPrincipal); // override
}
```

A programmatic WI caller does **not** send that header, so the principal stays a
raw **resource (workload) principal**. The handlers then send a `WorkspaceAccessType`
to the Lake authz service (`performRbacCheck` → `OCIDataLakeUtil` → `lakeUtil.checkRBAC`):

- **`PRIVILEGED_USER`** (used by CREATE) is a **role-membership** test — "is this
  principal a member of an admin/privileged role on the data lake?" Any principal
  type in `AI_DATA_PLATFORM_ADMIN` satisfies it → **CREATE passes**. *(inferred —
  Lake side)*
- **`USER`** (used by LIST + git pull) is a **user-identity / per-user-grant**
  test. A resource principal has no user identity / per-user grant → **fails**.
  *(inferred — Lake side)*

### Mechanism 2 — `gitFolderMetadata` uses a different (lighter) check

`gitFolderMetadata` calls `getPrimaryPrincipal()` then checks
`FolderAccessType.READ`/`FileAccessType.READ` (`GitResource.java:301,306`) — **not**
`WorkspaceAccessType.USER` — so it passes where the USER-typed git ops (pull, at
`:477`) fail.

### Mechanism 3 — `mkdir` never converts the principal at all

`WorkspaceObjectResource.mkdir` (`:691`) passes the **raw** principal straight to
`workspaceObjectService.createFolder(...)` → the backend Lake **volume** handler.
It does not call `getPrimaryPrincipal()` (the file's single call is at `:425`, in
a different method). The Lake volume handler expects a USER principal, so a raw
workload principal is rejected — this is the `VolumeRequestHandler …
isAccessGranted failed for CreateDirectory` error. (`VolumeRequestHandler` /
`isAccessGranted` have **0 matches in the `datahub` repo**, confirming this
denial is emitted Lake-side.)

### Why instance principals work everywhere (inferred)

OCI **instance** principals work for all of these under the same role. They are
presumably enriched to a user/principal context upstream (OCI API gateway / Lake
resolves an instance principal to a usable identity, or the `dh-user-principal`
header is supplied for them). This enrichment is **not visible in the `datahub`
repo** — inferred. **Workload** principals get no such enrichment and aren't
converted unless `dh-user-principal` is set, so they only clear the
role-membership (`PRIVILEGED_USER`) and folder-READ checks.

**Net:** the inconsistency is the sum of three things — most operations only clear
authorization for a *user-identity*, the workload principal isn't enriched to one
(no `dh-user-principal`), and `mkdir` doesn't even attempt the conversion. CREATE
and `gitFolderMetadata` happen to use checks (role-membership / folder-READ) that
a bare resource principal can satisfy.

---

## Requested fix

Make workload/resource-principal authorization **consistent across all
operations**. Concretely, addressing the three mechanisms above:

1. **Enrich the workload principal to a user identity for `USER`-typed checks.**
   Whatever makes an OCI *instance* principal satisfy `WorkspaceAccessType.USER`
   today (upstream enrichment / `dh-user-principal`) should also apply to
   *workload* principals — so LIST clusters/jobs and git pull resolve the
   workload principal's DG→role membership instead of failing for lack of a user
   identity.
2. **Call `getPrimaryPrincipal()` on the volume/`mkdir` path.**
   `WorkspaceObjectResource.mkdir` (`:691`) passes the raw principal to the Lake
   volume handler without conversion (unlike the cluster/git resources). It
   should convert/enrich the principal the same way, so the Lake volume handler
   receives a usable identity.
3. **Or: have `PRIVILEGED_USER`-style role-membership authorization apply to the
   `USER` and volume checks** for principals that are members of an admin role —
   i.e. don't require a per-user grant when the principal already holds the role.

Equivalently in one line: if a `dh-user-principal`-style enrichment is what makes
the working operations work, propagate it to **all** workspace operations
(including LIST, git pull, and `mkdir`), not just CREATE / `gitFolderMetadata`.

---

## Impact & current workaround

- **Impact:** OKE Workload Identity cannot be used as the end-to-end identity for
  an AIDP CI/CD reconcile (it dies at the first volume/list op).
- **Workaround (in use):** the OKE runner authenticates with the **node's
  instance principal** (its DG is in `AI_DATA_PLATFORM_ADMIN`) instead of WI —
  identical to how the VM runner works. This is selected by
  `deploy/aidp_deploy.py --runner oke` (see `RUNNER_AUTH`). The cost is that OKE
  buys ephemeral runners but **no identity advantage** over the VM.

Background and the full as-built journey: `docs/oke-runner-setup.md`.

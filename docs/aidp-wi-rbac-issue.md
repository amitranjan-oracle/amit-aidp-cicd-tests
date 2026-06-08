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

## Likely root cause (from code study — to confirm)

> This section is inferred from reading `IdeaProjects/datahub` and has **not**
> been confirmed at runtime by the AIDP team. File/line references are provided
> so it can be verified. Treat as a hypothesis.

The data-plane resolves the caller's principal in
`datahub-dp/api-handler/datahub-dp-api/.../utils/AuthUtil.java`,
`getPrimaryPrincipal()` (≈ lines 92–156). There is **special handling for
WORKLOAD / COMPUTE_CLUSTER resource principals** that swaps in a different
principal **only when a `dh-user-principal` HTTP header is present**:

```java
// If principal is workloadId, use the dh-user-principal if available
String dhUserPrincipal = httpHeaders.getHeaderString(DH_USER_PRINCIPAL_HEADER);
if (primaryPrincipal != null
        && primaryPrincipal.getClaimValue(ClaimType.RESOURCE_TYPE).isPresent()
        && (… .contains(WORKLOAD) || … .contains(COMPUTE_CLUSTER))
        && !StringUtils.isBlank(dhUserPrincipal)) {
    primaryPrincipal = getPrincipalFromBase64(dhUserPrincipal); // override
}
```

Different endpoints then ask for different RBAC permission *types*
(`WorkspaceAccessType`, verified against the source):

- **CREATE** cluster — `ClusterResource.createCluster()` (decl. line 215) checks
  `WorkspaceAccessType.PRIVILEGED_USER` at **line 325**.
- **LIST** clusters — `ClusterResource.listClusters()` (decl. line 1161) checks
  `WorkspaceAccessType.USER` at **line 1209**.
- **Git pull** — `GitResource.java` `performRbacCheck(…, WorkspaceAccessType.USER)`
  at **line 477** (every Git op in that file uses `USER`).

The principal is serialized to the backend Lake authorization service in
`OCIDataLakeUtil.serializePrincipal()` / `ServiceUtil.java`, and the response is
evaluated back in `AuthUtil.java`. The strings `VolumeRequestHandler` and
`isAccessGranted` have **0 matches anywhere in the `datahub` repo** (verified by
grep), so that particular denial originates in the **backend Lake/volume
service**, not in the AIDP API handler.

**Hypothesis:** when no `dh-user-principal` header is supplied (the normal case
for a programmatic WI caller), the raw workload principal reaches the
`USER`-permission-type and VolumeRequestHandler checks. The Lake authorization
path resolves **group/role membership for USER/INSTANCE principals but not for
WORKLOAD/resource principals**, so those `USER`-type and volume checks fail —
while the `PRIVILEGED_USER` path used by CREATE does not require that same
membership resolution and therefore passes. In short: workload-principal →
role-membership resolution is implemented for some permission types/handlers and
missing for others.

---

## Requested fix

Make workload/resource-principal authorization **consistent across all
operations**: resolve a workload principal's dynamic-group → role membership for
the `USER` permission-type checks (LIST clusters/jobs, git pull) and for the
VolumeRequestHandler checks (mkdir, git folder), the same way it already works
for the `PRIVILEGED_USER` checks (cluster/job CREATE) and for instance
principals. Equivalently: if a `dh-user-principal` enrichment is required for
workload principals, propagate it for **all** workspace operations, not just the
CREATE path.

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

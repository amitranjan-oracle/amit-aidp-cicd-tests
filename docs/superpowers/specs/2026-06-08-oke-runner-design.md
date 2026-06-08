# OKE-Hosted GitHub Runner for AIDP CI/CD — Design

**Goal:** Run the AIDP CI/CD reconcile (`deploy/aidp_deploy.py`) on a GitHub
self-hosted runner hosted in an **OKE Enhanced cluster** instead of (alongside)
the `amit-cicd-compute` VM, authenticating to the AIDP data plane via **OKE
Workload Identity** and to GitHub via a **PAT**.

**Architecture:** GitHub Actions Runner Controller (ARC, the GitHub-official
*runner scale set* charts) runs in a private OKE cluster placed in the **same
subnet as `amit-cicd-compute`**. Ephemeral runner pods authenticate to AIDP with
a per-ServiceAccount Workload-Identity resource principal; that workload is
authorized by adding its **dynamic group** to `AI_DATA_PLATFORM_ADMIN` as a
**GROUP member** (verified-working method). The existing VM runner is left
intact (coexistence); cutover is a later, documented step.

**Tech stack:** OKE (Enhanced, flannel CNI), Actions Runner Controller
(`gha-runner-scale-set` + controller Helm charts), Helm 3, kubectl, OCI CLI,
OCI Python SDK (Workload-Identity signer), GitHub Actions.

---

## 1. Relationship to the existing VM runner (coexistence)

The VM setup is untouched:
- `.github/workflows/cicd.yml` — VM workflow, `runs-on: [self-hosted, aidp]`.
- `deploy/cicd.yaml` — `git.credential_key` = `cicd-instance-principal`
  (`89e86bb7-5392-4a8c-a5ec-924c87546378`), owned by the **VM** instance
  principal.

This work **adds** a parallel path. Both reconcile the *same* AIDP target
(workspace, `cicd_01` cluster, `cicd_workflow_job`); they differ only in **how
the runner authenticates** (signer) and **which AIDP git credential** they use
(principal-owned). The OKE workflow is `workflow_dispatch`-only at first so push
to `main` does not double-reconcile. Cutover (flip triggers / retire the VM
runner) is explicitly **out of scope** for this branch.

## 2. Locked decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Cluster lifecycle | **Provision live** an Enhanced OKE cluster via a documented script + README (no Terraform). |
| 2 | Pod → AIDP auth | **OKE Workload Identity** (workload dynamic group → AIDP role GROUP member). |
| 3 | ARC → GitHub auth | **PAT** from OCI Vault secret `amitranjan-git-pat`, stored as a k8s Secret. |
| 4 | VM coexistence | **Coexist** — new `cicd-oke.yml`, VM workflow untouched. |
| 5 | Runner image / Python | **Stock** `ghcr.io/actions/actions-runner` (no custom OCIR image). The stock image is minimal and ships **no Python** and runs **non-root**, so Python is provisioned per-job by **`actions/setup-python@v5`** (root-less, pulls a self-contained build via NAT into the runner tool cache), then `pip install oci requests pyyaml`. |
| 6 | ARC flavor | **GitHub-official** `gha-runner-scale-set` (controller + scale set), **not** legacy summerwind ARC. |
| 7 | Network placement | **Same subnet** as `amit-cicd-compute` (`10.0.1.0/24`), private API endpoint, flannel overlay, OKE NSGs (shared security list untouched). |
| 8 | Workload DG | **I create it and output the OCID; the user adds it to `AI_DATA_PLATFORM_ADMIN`.** |

### ARC consequence — how `runs-on` works here
With `gha-runner-scale-set`, a job targets the **runner scale set's installation
name**, *not* an AND-matched label list. The OKE job uses
`runs-on: aidp-cicd-runners` (the Helm release / scale-set name). The legacy
`runs-on: [self-hosted, oke, aidp]` label model does **not** apply to this chart.

## 3. Network design (discovered, real values)

| Resource | OCID / value |
|---|---|
| Compartment `DataServices` | `ocid1.compartment.oc1..aaaaaaaaxtf7gpp5elpwzjub5odf5dapcvrrvvnytuupdvsk4x2hgb5v5zva` |
| Region / AD | `us-ashburn-1` / `yBdo:US-ASHBURN-AD-1` |
| VCN `dsvcn` | `ocid1.vcn.oc1.iad.amaaaaaaai22xpqaxoezzlk6wx2fyui4el6p453bpypzq5qoixtogr267glq` — `10.0.0.0/16` |
| Subnet `private subnet-dsvcn` (VM's) | `ocid1.subnet.oc1.iad.aaaaaaaap3sh5hnpfkv4eecgolm7inquxdks3j255x4duajpquyid2ni7pba` — `10.0.1.0/24`, **private** |
| Route table | `ocid1.routetable.oc1.iad.aaaaaaaaof63ev4zdktno5iy3dqnpr43vpe7qf6royrihyprroik6viwucda` — NAT (`0.0.0.0/0`) + Service Gateway (`all-iad-services`) |
| VM `amit-cicd-compute` | `ocid1.instance.oc1.iad.anuwcljtai22xpqcdr4qqkkxcj3i3ixir5cqhieu4fbqpftctr6dfazb3dha` — `10.0.1.84`, **no NSG** |

**Placement choices and why:**
- **Same subnet (`10.0.1.0/24`)** for both the **private API endpoint** and the
  **node pool**, per the directive. The subnet already routes egress via **NAT**
  (GitHub long-poll, `ghcr.io` runner image, PyPI) and reaches OCI services via
  the **Service Gateway** — no new gateways needed.
- **Flannel overlay CNI** (not VCN-native): pod IPs come from an overlay
  (`10.244.0.0/16`), so OKE does **not** consume scarce host IPs from the shared
  `/24`. VCN-native would require a large dedicated pod subnet — rejected.
- **NSGs, not security-list edits.** Create two NSGs and attach them to OKE
  resources, leaving the subnet's shared security list (which also governs the
  VM) untouched:
  - `oke-k8s-api-nsg` — control-plane endpoint rules.
  - `oke-node-nsg` — worker node rules (node↔control-plane, node↔node,
    NodePort/health-check, egress to NAT + SGW + ICMP path-MTU).
  Rules follow OCI's documented "private cluster, flannel" matrix.
- **Pod/Service CIDRs** (`10.244.0.0/16` pods, `10.96.0.0/16` services) do not
  overlap the VCN `10.0.0.0/16` at the routing layer because flannel pod traffic
  is overlay-encapsulated; service CIDR is cluster-internal only.

**kubectl reachability:** the API endpoint is private (in `10.0.1.0/24`).
`amitdemografana` (`10.0.0.54`, `public subnet-dsvcn`) is in the **same VCN**, so
it reaches the endpoint via intra-VCN routing — `kubectl`/`helm` run **directly
on `amitdemografana`** (no second hop to `amit-cicd-compute`). The API NSG allows
ingress on 6443 from the public subnet (`10.0.0.0/24`). `amitdemografana` needs
OCI CLI auth (instance principal or config) to run
`oci ce cluster create-kubeconfig` + `generate-token`.

### Cluster shape (minimal / cost-conscious)
- **Names** (theme `aidp-cicd-test` — testing AIDP CI/CD via GitHub): cluster
  `aidp-cicd-test`; node pool `aidp-cicd-test-np`; NSGs `aidp-cicd-test-api-nsg`
  / `aidp-cicd-test-node-nsg`; workload DG `aidp-cicd-test-workload-dg`; runner
  scale set `aidp-cicd-runners` (= the `runs-on` value); namespaces `arc-systems`
  / `arc-runners`; ServiceAccount `aidp-runner-sa`.
- **Type: Enhanced** (required for Workload Identity).
- **Kubernetes: v1.34.2** (node pool version ≤ control-plane version).
- **Node pool:** **1 node, `VM.Standard.E4.Flex`, 1 OCPU / 8 GB**, AD-1, OL8 OKE
  worker image, in `10.0.1.0/24`. Cheapest shape that reliably runs the OKE
  system pods + ARC controller/listener + one short-lived runner pod (no heavy
  workload here). Bump memory first if a runner can't schedule. README documents
  scaling the node pool to 0 / deleting the cluster when idle to stop billing.

## 4. Identity & auth model

### 4.1 Pod → AIDP (Workload Identity)
- Runner pods run under ServiceAccount **`aidp-runner-sa`** in namespace
  **`arc-runners`**.
- A **workload-type dynamic group** matches that workload:
  ```
  ALL {resource.type = 'workload',
       resource.compartment.id = '<DataServices compartment ocid>',
       resource.k8s.cluster.id = '<new cluster ocid>',
       resource.k8s.namespace.name = 'arc-runners',
       resource.k8s.serviceaccount.name = 'aidp-runner-sa'}
  ```
  > ⚠️ The exact attribute key spellings (`resource.k8s.namespace.name` /
  > `resource.k8s.serviceaccount.name` / `resource.k8s.cluster.id`) must be
  > confirmed against current OCI Workload-Identity docs; the create command
  > validates them and we iterate if rejected.
- **Authorization:** add this dynamic group to **`AI_DATA_PLATFORM_ADMIN`** as a
  **GROUP member** (`aidp_roles add_member member_type=GROUP target=<dg-ocid>`)
  — the verified-working method (the VM's `DataServices-Compute-DG` is already a
  GROUP assignee on that role). *I create the DG and output its OCID; the user
  performs the role add.* (Least-privilege scoped role noted as a future option.)
- **In code:** `deploy/aidp_deploy.py` obtains
  `oci.auth.signers.get_oke_workload_identity_resource_principal_signer()` when
  `AIDP_AUTH_METHOD=oke_workload_identity` is set.

### 4.2 Pod → GitHub (PAT)
- ARC needs a **renewable** credential (it mints registration tokens to create
  ephemeral runners), unlike the VM's one-shot registration token. We use the
  PAT from Vault secret `amitranjan-git-pat`
  (`ocid1.vaultsecret.oc1.iad.amaaaaaaai22xpqatzdboqsmngy72nhogsg32okj63o6h2ex2mwahvzfxqsq`),
  stored as a k8s Secret referenced by the runner scale set.
- The PAT must have **`repo`** scope (covers both AIDP server-side clone and
  repo-level ARC registration). Scope is verified before bootstrap.

### 4.3 Why a separate AIDP git credential (`cicd-workload-principal`)
- AIDP performs git-folder clone/pull **server-side**, authenticated by a
  **GIT_ACCOUNT userSetting** referenced via `credential_key`, resolved **in the
  caller's identity context**. AIDP cannot read an OCI Vault secret directly.
- The OKE workload principal is a **different** principal than the VM instance
  principal, so the VM's `cicd-instance-principal` credential is **invisible** to
  it (would `InternalError` on clone — the bug already burned us once).
- Therefore create a new GIT_ACCOUNT setting **`cicd-workload-principal`**, and
  it **must be created under Workload Identity (from a pod)** so AIDP records the
  workload principal as owner. The PAT value is sourced from the same Vault
  secret. **The same token backs all three stores** (Vault = canonical, k8s
  Secret = runner, AIDP userSetting = backend); only the *storage* differs.
- **Sequencing:** cluster → WI/DG/role → `init-aidp-credential` (WI pod creates
  `cicd-workload-principal`) → the OKE runner resolves it **by name** via
  `AIDP_GIT_CREDENTIAL_NAME=cicd-workload-principal` (no key copying).

## 5. Component / file layout

```
oke/
  provision-cluster.sh        # OCI CLI: NSGs + Enhanced cluster (private API, flannel)
                              #          + node pool, all in 10.0.1.0/24; prints cluster OCID
  create-workload-dg.sh       # oci iam dynamic-group create (WI rule) -> prints DG OCID
  bootstrap-runner.sh         # create-kubeconfig; ns + SA; PAT k8s Secret (from Vault);
                              #   helm install ARC controller + runner scale set
  init-aidp-credential.sh     # kubectl-run a WI pod that creates cicd-workload-principal
  namespaces.yaml             # arc-systems, arc-runners
  runner-serviceaccount.yaml  # aidp-runner-sa (the WI subject)
  values-controller.yaml      # gha-runner-scale-set-controller Helm values
  values-runnerset.yaml       # gha-runner-scale-set values: repo URL, PAT secret ref,
                              #   scaleSetName=aidp-cicd-runners, min/maxRunners,
                              #   template.spec.serviceAccountName=aidp-runner-sa (WI subject)
  config.env                  # operator-edited vars (OCIDs, names) sourced by the scripts
docs/oke-runner-setup.md      # README/runbook: prereqs, network/security, ordered steps, verify
.github/workflows/cicd-oke.yml# workflow_dispatch; runs-on: aidp-cicd-runners;
                              #   env AIDP_AUTH_METHOD + AIDP_GIT_CREDENTIAL_NAME;
                              #   actions/setup-python -> pip install -> reconcile
deploy/aidp_deploy.py         # + WI signer branch + AIDP_GIT_CREDENTIAL_KEY override (VM unchanged)
```

The scripts are **thin and heavily commented** — each runs the exact commands the
README documents (the README is the source of truth). `config.env` centralizes
the discovered OCIDs so the scripts have no magic literals.

## 6. Change to `deploy/aidp_deploy.py` (minimal, VM-safe)

- `build_signer()` gains a branch: if `os.environ.get("AIDP_AUTH_METHOD") ==
  "oke_workload_identity"`, return
  `oci.auth.signers.get_oke_workload_identity_resource_principal_signer()`
  (guarded by `ImportError`, like the existing RP guard).
- The AIDP git credential is resolved **by name** at runtime: when env
  `AIDP_GIT_CREDENTIAL_NAME` is set, the script queries
  `GET userSettings?settingType=GIT_ACCOUNT` under the current principal and uses
  the `key` of the setting with that `name`; otherwise it uses the yaml
  `git.credential_key`. This avoids copying a freshly-generated credential key
  into config — the OKE workflow just names `cicd-workload-principal`.
- **VM path is byte-for-byte unchanged:** no env set ⇒ existing RP→instance-
  principal detection and the yaml `credential_key`. Single source of truth for
  the AIDP *target* stays in `deploy/cicd.yaml`; only the two principal-specific
  knobs (`AIDP_AUTH_METHOD`, `AIDP_GIT_CREDENTIAL_NAME`) are env-set on OKE.

## 7. Security considerations

- Private cluster, private nodes, no public IPs, no inbound LB — runners are
  outbound-only (long-poll). Matches the VM's threat model.
- OKE NSGs scope traffic to OKE resources; the VM's (NSG-less) posture is
  unchanged because we don't edit the shared security list.
- The OKE workflow triggers only on `workflow_dispatch`/`push` to `main`, never
  `pull_request` from forks (no untrusted code on the runner).
- PAT lives in Vault and a k8s Secret; never echoed/committed. README forbids
  printing it.
- Workload DG is scoped to one cluster + namespace + ServiceAccount, so only the
  runner pods (not every pod, not the nodes) get the AIDP-admin identity.

## 8. Testing & verification

Live end-to-end is performed after provisioning:
1. `kubectl get pods -n arc-systems` (controller Running) and
   `kubectl get pods -n arc-runners` (listener Running, 0 ephemeral runners idle).
2. WI smoke test: a one-off pod under `aidp-runner-sa` runs
   `InstancePrincipalsSecurityTokenSigner`-equivalent WI signer and lists AIDP
   workspaces — proves the DG→role authorization works.
3. `init-aidp-credential` creates `cicd-workload-principal`; verify via
   `GET userSettings?settingType=GIT_ACCOUNT` under WI lists it.
4. Trigger `cicd-oke.yml` (`workflow_dispatch`) → watch an ephemeral runner pod
   spawn, the reconcile run, then pod terminate.
5. Verify via MCP that the git folder pulled and `cicd_workflow_job`/`cicd_01`
   are in-sync (idempotent no-op if the VM already converged them).

**Honest limitation:** step 2 onward depends on the user having added the DG to
`AI_DATA_PLATFORM_ADMIN` (decision #8). Until then, AIDP calls from the pod will
401/403 and the runbook says so. Any step blocked on that is reported as
**pending the role add**, not as passed.

## 9. Risks & open items

- **DG matching-rule key names** — confirm exact spelling at create time
  (§4.1 note); iterate on rejection.
- **PAT scope** — confirm `repo` (and that repo-level ARC accepts it) before
  bootstrap; if ARC needs broader scope, surface it rather than silently failing.
- **WI signer prerequisites** — Enhanced cluster + supported k8s version + the
  projected SA token; if the signer can't bootstrap in-pod, capture the error and
  diagnose (don't conclude "principal lacks privilege" prematurely — that was a
  past false lead).
- **k8s version drift** — pin control-plane and node-pool versions explicitly in
  `config.env`.
- **Cost** — a running node pool bills continuously (unlike the VM-only setup).
  README notes how to scale the node pool to 0 / delete the cluster.

## 10. Out of scope

- Decommissioning the VM runner / cutover (later).
- Terraform / full IaC (scripts + README only).
- Custom OCIR runner image.
- Cluster autoscaler, multi-AD, HA control plane tuning beyond OKE defaults.
- A least-privilege AIDP role (use `AI_DATA_PLATFORM_ADMIN`; note as future work).

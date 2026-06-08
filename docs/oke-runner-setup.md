# OKE-hosted GitHub runner for AIDP CI/CD — setup & runbook

A GitHub self-hosted runner running in **OKE** (via Actions Runner Controller)
that executes the AIDP reconcile (`deploy/aidp_deploy.py`), **coexisting** with
the `amit-cicd-compute` VM runner. Verified working end-to-end on 2026-06-08.

> **TL;DR of how it actually works (and what didn't):** managed node pools and
> OKE Workload Identity both turned out to be dead ends in this tenancy (see
> **Findings**). The working setup is a **self-managed OKE node** that runs real
> `kube-proxy`, with the reconcile authenticating to AIDP via the **node's
> instance principal** (`DataServices-Compute-DG`) — the same identity model as
> the VM runner. OKE buys ephemeral/scale-to-zero runners here, **not** an
> identity advantage.

---

## Architecture (final)

```
GitHub push/dispatch ─▶ ARC listener (arc-systems) ─▶ ephemeral runner pod (arc-runners)
                                                          │ on the self-managed node
                                                          ▼
   actions/checkout → setup-python → pip → python deploy/aidp_deploy.py --config deploy/cicd.yaml
                                                          │
                                   AIDP auth = NODE INSTANCE PRINCIPAL (via node IMDS)
                                   = DataServices-Compute-DG  (authorized for AIDP volume ops)
                                                          ▼
   Phase 0 ensure GIT_ACCOUNT cred (from OCI Vault secret) · Phase 1 mkdir ·
   Phase 2 clone/pull the OKE git folder · Phase 3 cluster cicd_01 · Phase 4 job cicd_workflow_job
```

- **Cluster:** Enhanced OKE, **flannel** CNI, **v1.36.0**, private API endpoint,
  in the VM's VCN `dsvcn` / subnet `10.0.1.0/24` (DataServices compartment).
- **Node:** ONE **self-managed** node launched from an OKE worker image with
  `areLegacyImdsEndpointsDisabled=true` (IMDSv2). A real node ⇒ `kube-proxy` ⇒
  ClusterIP/DNS work (virtual nodes don't provide this).
- **Runner:** ARC `gha-runner-scale-set` named **`amit-cicd-oke`** (= the
  `runs-on:` value), ephemeral pods, min 0 / max 2.
- **AIDP auth:** the **node instance principal** (the pod reaches node IMDS).
  `DataServices-Compute-DG` (which the node matches) is in `AI_DATA_PLATFORM_ADMIN`
  and is authorized for workspace-volume ops.
- **Git folder:** the OKE runner uses its **own** folder
  `/Workspace/cicd_folder/amit-aidp-cicd-tests-oke` — per-instance-principal
  credential ownership means the OKE node can't pull the VM's folder (and
  vice-versa), so each runner owns a distinct folder. Set via the
  `AIDP_FOLDER_PATH` / `AIDP_PARENT_DIR` env in `cicd-oke.yml`.

## Findings (why this shape — three dead ends)

1. **Managed node pools — blocked by tenancy IMDSv2.** The tenancy enforces
   `imds/imds-disable-v1`; OKE node pools expose no IMDS knob, so node launch is
   rejected (`Invalid instanceOptions.areLegacyImdsEndpointsDisabled: false`).
   A self-managed node *launched as a plain instance* CAN set IMDSv2, sidestepping it.
2. **Virtual nodes — no kube-proxy.** A virtual-node-only cluster has no
   `kube-proxy` DaemonSet, so **ClusterIP and in-cluster DNS don't work** → the
   ARC controller can't reach the API and runner pods have no DNS. (WI auth +
   even mkdir-able? No — see #3.) A *real* node fixes this.
3. **OKE Workload Identity — not authorized for AIDP workspace volume ops.** WI
   authenticates fine for AIDP *data-lake* ops (it created the GIT_ACCOUNT
   credential and read the Vault secret), but AIDP's `VolumeRequestHandler`
   **RBAC-denies WI principals** (`mkdir`/git-folder → HTTP 404 "isAccessGranted
   failed for CreateDirectory"). Moving the workload DG to the
   OracleIdentityCloudService domain did **not** help (not a domain issue). The
   **node instance principal IS authorized** (verified: `mkdir` → HTTP 409
   "already exists"). ⇒ Use the node instance principal; WI gives no usable
   advantage for AIDP here.

## Network / security / access

- **Private API endpoint** (`10.0.1.x:6443`). Two NSGs (not the shared security
  list): `aidp-cicd-test-api-nsg` (control plane) + `aidp-cicd-test-node-nsg`
  (node/pods) with the OCI flannel-private-cluster rule matrix. The API NSG also
  allows `6443` from the cluster subnet `10.0.1.0/24` (kubectl from
  `amit-cicd-compute`) and the bastion subnet `10.0.0.0/24` (amitdemografana).
- **Egress reality:** `amitdemografana` (public subnet) has **restricted egress**
  — it reaches OCI services but NOT `github.com`/`ghcr.io`/`get.helm.sh`.
  `amit-cicd-compute` (private subnet, NAT) has **full egress** (its runner
  long-polls GitHub), so **run kubectl/helm/oci from `amit-cicd-compute`** (helm
  pulls the ARC charts + the node/runner images pull from ghcr.io via the pod
  subnet's NAT).
- **Tooling on `amit-cicd-compute`:** install the **self-contained oci CLI**
  (`raw.githubusercontent.com/oracle/oci-cli/.../install.sh` → `~/bin/oci`; the
  box has no `pip`). `kubectl` + `helm`: the box reaches dl.k8s.io/github
  releases via NAT, or scp the linux binaries from a machine with internet.
- **SSH path:** Mac → `amitdemografana` (`144.25.95.237`) →
  `amit-cicd-compute` (`10.0.1.84`) via `ProxyCommand`, key
  `ssh-key-2025-08-29.key` (authorized on both).
- **kubelet auth:** the self-managed node's kubelet authenticates with the same
  `oci ce cluster generate-token` path its instance principal already uses — no
  extra IAM policy needed.

## Setup steps (scripts in `oke/`, config in `oke/config.env`)

1. **Provision cluster** (from a host with OCI CLI + DEFAULT profile, e.g. the Mac):
   `./oke/provision-cluster.sh` → NSGs + flannel Enhanced cluster (no node pool).
2. **Launch the self-managed node** (from the Mac):
   `SSH_PUBKEY=/path/to/key.pub ./oke/launch-self-managed-node.sh` → launches an
   OKE-image instance with IMDSv2; cloud-init runs
   `/etc/oke/oke-install.sh --apiserver-endpoint <IP-ONLY> --kubelet-ca-cert <b64>`.
   The node registers Ready; `kube-proxy` becomes 1/1 and CoreDNS goes Running.
   - **Gotchas:** the endpoint must be the **IP only** (`oke-install` appends
     ports; `ip:6443` → bootstrap URL `ip:6443:12250` → "no such host").
     `oke-install` resets networking (drops SSH) → if running it by hand, run
     **detached** (`setsid … </dev/null &`). The bootstrap script is
     `/etc/oke/oke-install.sh` (NOT `/usr/libexec/oke/oke-init.sh`).
3. **Workload DG** (`./oke/create-workload-dg.sh`) — created for the WI
   experiment; **not required** for the final node-instance-principal design.
   (Kept for the record / if AIDP later supports WI for volumes.)
4. **Bootstrap ARC** (on `amit-cicd-compute`, after staging `oke/`):
   `./oke/bootstrap-runner.sh` → kubeconfig (private endpoint), namespaces +
   `aidp-runner-sa`, PAT k8s Secret from the Vault, `helm upgrade --install` the
   controller + the `amit-cicd-oke` runner scale set. Runner registers online.
5. **Run it:** trigger `aidp-cicd-oke` (`workflow_dispatch` once the workflow is
   on `main`, or via a temporary branch `push:` trigger for testing). An
   ephemeral runner pod spawns on the node, runs the reconcile against the OKE
   git folder, then terminates.

> `oke/init-aidp-credential.sh` (a WI pod that pre-creates the credential from
> the k8s Secret) is only needed for the **Workload Identity** variant. With the
> node instance principal, `aidp_deploy.py` **phase 0** creates the GIT_ACCOUNT
> credential directly (the node principal can read the Vault secret).

## Tooling gotchas (for reproducers)

- Bastion can't reach ghcr.io/github → use `amit-cicd-compute` for helm/charts.
- `amit-cicd-compute` has no `pip` → install the self-contained oci CLI; scp
  kubectl/helm linux binaries from a machine with internet.
- Runner image is **non-root** → clone to `/tmp/...`; system Python is
  externally-managed → `pip3 install --break-system-packages` (the workflow uses
  `actions/setup-python`, whose Python doesn't need that flag).
- Identity-domain DG `matchingRule` is **omitted from GET/LIST by default** —
  fetch with `attributes=matchingRule` or you'll wrongly think it didn't persist.

## Coexistence with the VM runner

| | VM runner | OKE runner |
|---|---|---|
| workflow | `cicd-vm.yml` (`aidp-cicd-vm`) | `cicd-oke.yml` (`aidp-cicd-oke`) |
| `runs-on` | `[self-hosted, aidp, vm]` | `amit-cicd-oke` |
| AIDP auth | VM instance principal | node instance principal |
| git folder | `…/amit-aidp-cicd-tests` | `…/amit-aidp-cicd-tests-oke` |

Both reconcile the **same** `cicd_01` cluster + `cicd_workflow_job` (idempotent
NO-OP when already in sync); only the git folder differs (credential ownership).

## As-built (live, 2026-06-08, DataServices / us-ashburn-1)

| Resource | OCID / value |
|---|---|
| Cluster `aidp-cicd-test` (flannel, v1.36.0) | `ocid1.cluster.oc1.iad.aaaaaaaaheuhoa3ik57tqef2dapleyotegmbcenb2pplwkzukchuqwarlo4q` |
| Private API endpoint | `10.0.1.180:6443` |
| Self-managed node `aidp-cicd-test-node` | `ocid1.instance.oc1.iad.anuwcljtai22xpqcs7eocwvx6rso2pkrig44gvkppkvyrm2hxkndqgdavbka` (`10.0.1.237`) |
| API NSG | `ocid1.networksecuritygroup.oc1.iad.aaaaaaaa6pymbo62yw5wbcjkboj4ffuatykdsppjel2rwn7oqqnho543wkfq` |
| Node NSG | `ocid1.networksecuritygroup.oc1.iad.aaaaaaaa3f4rzrrm6lcfnuntpf4zm7aiphnmttaeokh7h6y4zdaynl3tcyyq` |
| Workload DG (IDCS; unused by final design) | `ocid1.dynamicgroup.oc1..aaaaaaaa6i75hgee2dk3tly2yjrf2ayksibtnjuulm4oxyhonimvf2g5gilq` |
| ARC runner scale set | `amit-cicd-oke` (controller in `arc-systems`, runners in `arc-runners`) |

## Teardown / cost

A self-managed node + Enhanced control plane bill continuously. To stop:
```
oci compute instance terminate --instance-id <self-managed-node> --force
oci ce cluster delete --cluster-id <cluster> --force
# (optionally) delete the two NSGs; the workload DG/grafana DG live in OracleIdentityCloudService
```
Everything is reproducible from `oke/*.sh` + `oke/config.env`.

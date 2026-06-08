#!/usr/bin/env bash
# Launch a fresh SELF-MANAGED OKE worker node and join it to the (flannel) cluster.
#
# Why this shape of solution (see docs/oke-runner-setup.md "Node strategy"):
#   - Managed node pools can't launch in this tenancy (enforced IMDSv2; OKE node
#     pools expose no legacy-IMDS knob).
#   - Virtual-node-only clusters have no kube-proxy, so ClusterIP/DNS break (ARC
#     can't run).
#   - A self-managed node launched FROM an OKE worker image has the node software
#     baked in AND can disable legacy IMDS at *instance launch* (instanceOptions),
#     sidestepping the tenancy block. A real node runs kube-proxy => services work.
#
# Run from any host with the oci CLI + access to launch instances (e.g. the Mac
# with the DEFAULT profile). The node joins via cloud-init (oke-init) and its
# kubelet authenticates with the same token path its instance principal already
# uses (DataServices-Compute-DG), so no extra IAM policy is needed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a

NODE_NAME="${SELF_MANAGED_NODE_NAME:-aidp-cicd-test-node}"
SM_SHAPE="${SELF_MANAGED_NODE_SHAPE:-VM.Standard.E4.Flex}"
SM_OCPUS="${SELF_MANAGED_NODE_OCPUS:-2}"
SM_MEM="${SELF_MANAGED_NODE_MEM_GB:-16}"
SM_BOOT="${SELF_MANAGED_NODE_BOOT_GB:-50}"

# 1. OKE worker image matching the cluster k8s version (OL8, x86, non-GPU)
IMAGE=$(oci ce node-pool-options get --node-pool-option-id all --region "$REGION" \
  --query 'data.sources[].{name:"source-name",image:"image-id"}' --output json 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);v='$K8S_VERSION'.lstrip('v');m=[r for r in d if v in r['name'] and 'Oracle-Linux-8' in r['name'] and 'aarch64' not in r['name'] and 'GPU' not in r['name']];print(sorted(m,key=lambda r:r['name'])[-1]['image'] if m else '')")
[ -z "$IMAGE" ] && { echo "no OKE OL8 image for $K8S_VERSION"; exit 1; }
echo "OKE worker image: $IMAGE"

# 2. cluster private apiserver endpoint + base64 CA (for the kubelet TLS bootstrap).
#    oke-install wants the endpoint HOST ONLY (it appends ports); the API returns
#    "<ip>:6443", so strip the port or the bootstrap URL becomes "<ip>:6443:12250".
EP=$(oci ce cluster get --cluster-id "$CLUSTER" --region "$REGION" \
  --query 'data.endpoints."private-endpoint"' --raw-output | cut -d: -f1)
oci ce cluster create-kubeconfig --cluster-id "$CLUSTER" --region "$REGION" \
  --file /tmp/kc-ca --token-version 2.0.0 --kube-endpoint PRIVATE_ENDPOINT --overwrite >/dev/null 2>&1
CA=$(grep 'certificate-authority-data:' /tmp/kc-ca | awk '{print $2}'); rm -f /tmp/kc-ca
echo "apiserver endpoint: $EP"

# 3. cloud-init: the OKE worker image ships the bootstrap at /etc/oke/oke-install.sh
#    (NOT /usr/libexec/oke/oke-init.sh). Join the flannel cluster.
CI=$(mktemp)
cat > "$CI" <<EOF
#!/usr/bin/env bash
bash /etc/oke/oke-install.sh --apiserver-endpoint "$EP" --kubelet-ca-cert "$CA"
EOF

# 4. launch the instance: in the cluster subnet, NODE_NSG, NO public IP, and
#    crucially IMDSv2-only via instanceOptions (this is what node pools can't set).
oci compute instance launch \
  --compartment-id "$COMPARTMENT_OCID" --availability-domain "$AVAILABILITY_DOMAIN" \
  --display-name "$NODE_NAME" --shape "$SM_SHAPE" \
  --shape-config "{\"ocpus\":$SM_OCPUS,\"memoryInGBs\":$SM_MEM}" \
  --image-id "$IMAGE" --subnet-id "$SUBNET_OCID" --nsg-ids "[\"$NODE_NSG\"]" \
  --assign-public-ip false \
  --boot-volume-size-in-gbs "$SM_BOOT" \
  --instance-options '{"areLegacyImdsEndpointsDisabled": true}' \
  --user-data-file "$CI" \
  ${SSH_PUBKEY:+--ssh-authorized-keys-file $SSH_PUBKEY} \
  --region "$REGION" --wait-for-state RUNNING --query "data.id" --raw-output > /tmp/smnode.id
rm -f "$CI"
INSTANCE=$(cat /tmp/smnode.id)
echo "SELF_MANAGED_NODE=$INSTANCE" >> "$HERE/state.env"
echo "launched node instance $INSTANCE; cloud-init runs oke-init to join."
echo "verify in ~3-6 min:  kubectl get nodes"

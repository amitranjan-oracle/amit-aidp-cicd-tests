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
# protocol 6=TCP, 1=ICMP, all=all. Each rule carries its own direction, so
# ingress+egress go in one `add` call. Clear existing rules first for idempotency
# (a fresh NSG needs `add`; `update` is for editing existing rules by id).
clear_rules(){
  local ids; ids=$(oci network nsg rules list --nsg-id "$1" --region "$REGION" --all \
    --query "data[].id" --output json 2>/dev/null || echo "[]")
  if [ -n "$ids" ] && [ "$ids" != "[]" ]; then
    oci network nsg rules remove --nsg-id "$1" --region "$REGION" --security-rule-ids "$ids" >/dev/null
  fi
}
clear_rules "$API_NSG"
oci network nsg rules add --nsg-id "$API_NSG" --region "$REGION" --security-rules "$(cat <<JSON
[
 {"direction":"INGRESS","protocol":"6","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","tcpOptions":{"destinationPortRange":{"min":6443,"max":6443}},"description":"workers to k8s API"},
 {"direction":"INGRESS","protocol":"6","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","tcpOptions":{"destinationPortRange":{"min":12250,"max":12250}},"description":"workers to control plane"},
 {"direction":"INGRESS","protocol":"1","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU from workers"},
 {"direction":"INGRESS","protocol":"6","source":"$BASTION_SUBNET_CIDR","sourceType":"CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":6443,"max":6443}},"description":"kubectl from amitdemografana subnet"},
 {"direction":"EGRESS","protocol":"6","destination":"$NODE_NSG","destinationType":"NETWORK_SECURITY_GROUP","description":"control plane to workers (all TCP incl 10250)"},
 {"direction":"EGRESS","protocol":"1","destination":"$NODE_NSG","destinationType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU to workers"},
 {"direction":"EGRESS","protocol":"6","destination":"$OSN_DEST","destinationType":"SERVICE_CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":443,"max":443}},"description":"control plane to OCI services via SGW"}
]
JSON
)"
clear_rules "$NODE_NSG"
oci network nsg rules add --nsg-id "$NODE_NSG" --region "$REGION" --security-rules "$(cat <<JSON
[
 {"direction":"INGRESS","protocol":"all","source":"$NODE_NSG","sourceType":"NETWORK_SECURITY_GROUP","description":"node to node"},
 {"direction":"INGRESS","protocol":"6","source":"$API_NSG","sourceType":"NETWORK_SECURITY_GROUP","description":"control plane to workers (all TCP incl 10250)"},
 {"direction":"INGRESS","protocol":"1","source":"$API_NSG","sourceType":"NETWORK_SECURITY_GROUP","icmpOptions":{"type":3,"code":4},"description":"path MTU from control plane"},
 {"direction":"INGRESS","protocol":"1","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","icmpOptions":{"type":3,"code":4},"description":"path MTU from internet"},
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
# `oci ce cluster create` is async (returns a work request); `--wait-for-state
# ACTIVE` is INVALID on a work-request response. Submit, then poll the cluster
# itself to ACTIVE. find_cluster matches any non-deleted cluster of that name
# (so a re-run picks up one already CREATING).
find_cluster(){ oci ce cluster list -c "$COMPARTMENT_OCID" --region "$REGION" --name "$CLUSTER_NAME" \
  --query "data[?\"lifecycle-state\"!='DELETED' && \"lifecycle-state\"!='DELETING'].id | [0]" --raw-output 2>/dev/null || true; }
CLUSTER=$(find_cluster); [ "$CLUSTER" = "null" ] && CLUSTER=""
if [ -z "$CLUSTER" ]; then
  oci ce cluster create \
    --compartment-id "$COMPARTMENT_OCID" --name "$CLUSTER_NAME" --vcn-id "$VCN_OCID" \
    --kubernetes-version "$K8S_VERSION" --type ENHANCED_CLUSTER \
    --endpoint-subnet-id "$SUBNET_OCID" --endpoint-nsg-ids "[\"$API_NSG\"]" \
    --endpoint-public-ip-enabled false \
    --pods-cidr "$POD_CIDR" --services-cidr "$SERVICE_CIDR" \
    --cluster-pod-network-options '[{"cniType":"FLANNEL_OVERLAY"}]' \
    --region "$REGION" >/dev/null
  for i in $(seq 1 60); do CLUSTER=$(find_cluster); [ -n "$CLUSTER" ] && [ "$CLUSTER" != "null" ] && break; sleep 5; done
fi
echo "CLUSTER=$CLUSTER" >> "$STATE"
log "CLUSTER=$CLUSTER — waiting for ACTIVE"
for i in $(seq 1 120); do
  ST=$(oci ce cluster get --cluster-id "$CLUSTER" --region "$REGION" --query "data.\"lifecycle-state\"" --raw-output 2>/dev/null || true)
  printf '  cluster state=%s\n' "$ST"
  [ "$ST" = "ACTIVE" ] && break
  [ "$ST" = "FAILED" ] && { echo "cluster FAILED"; exit 1; }
  sleep 15
done

# --- 4. Node image for k8s version + shape (OL8, x86, non-GPU) ---
# jmespath `contains()==false` is unreliable here; filter the source list in python
# and pick the newest matching image (names sort lexicographically by date).
IMAGE=$(oci ce node-pool-options get --node-pool-option-id all --region "$REGION" \
  --query 'data.sources[].{name:"source-name",image:"image-id"}' --output json 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); m=[r for r in d if '$K8S_VERSION'.lstrip('v') in r['name'] and 'Oracle-Linux-8' in r['name'] and 'aarch64' not in r['name'] and 'GPU' not in r['name']]; print(sorted(m,key=lambda r:r['name'])[-1]['image'] if m else '')")
[ -z "$IMAGE" ] && { echo "no OL8 x86 node image found for $K8S_VERSION"; exit 1; }
log "node image = $IMAGE"
echo "NODE_IMAGE=$IMAGE" >> "$STATE"

# --- 5. Node pool (1 node, small) — also async; submit then find by name ---
find_np(){ oci ce node-pool list -c "$COMPARTMENT_OCID" --cluster-id "$CLUSTER" --region "$REGION" \
  --name "$NODE_POOL_NAME" --query "data[0].id" --raw-output 2>/dev/null || true; }
NP=$(find_np); [ "$NP" = "null" ] && NP=""
if [ -z "$NP" ]; then
  oci ce node-pool create \
    --cluster-id "$CLUSTER" --compartment-id "$COMPARTMENT_OCID" --name "$NODE_POOL_NAME" \
    --kubernetes-version "$K8S_VERSION" --node-shape "$NODE_SHAPE" \
    --node-shape-config "{\"ocpus\":$NODE_OCPUS,\"memoryInGBs\":$NODE_MEM_GB}" \
    --node-image-id "$IMAGE" --size "$NODE_COUNT" \
    --node-boot-volume-size-in-gbs "$NODE_BOOT_GB" \
    --placement-configs "[{\"availabilityDomain\":\"$AVAILABILITY_DOMAIN\",\"subnetId\":\"$SUBNET_OCID\"}]" \
    --nsg-ids "[\"$NODE_NSG\"]" \
    --cni-type FLANNEL_OVERLAY \
    --region "$REGION" >/dev/null
  for i in $(seq 1 30); do NP=$(find_np); [ -n "$NP" ] && [ "$NP" != "null" ] && break; sleep 5; done
fi
echo "NODE_POOL=$NP" >> "$STATE"
log "NODE_POOL=$NP — nodes provisioning (poll: oci ce node-pool get --node-pool-id $NP)"
log "Done. state.env:"; cat "$STATE"

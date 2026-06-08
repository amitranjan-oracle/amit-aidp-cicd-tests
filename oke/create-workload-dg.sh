#!/usr/bin/env bash
# Create the OKE Workload-Identity dynamic group and print its OCID.
# Requires oke/state.env (CLUSTER). Run from a host with OCI CLI + DEFAULT profile.
# The matching-rule attribute keys are validated by the API; if create is
# rejected with "invalid attribute", adjust per current OKE Workload-Identity docs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$HERE/config.env"; . "$HERE/state.env"; set +a

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

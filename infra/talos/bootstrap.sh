#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — build a Talos v1.14 / Kubernetes 1.37 cluster with no internet
#
# Book:    Sovereign AI Stacks, Chapter 4
# Runs on: the enclave admin workstation (talosctl 1.14.4, kubectl, helm 3.22)
#
# Usage:   ./bootstrap.sh cluster.env            # full build
#          ./bootstrap.sh cluster.env render     # only render configs (no nodes touched)
#
# Steps:
#   1. generate cluster secrets once (kept in out/secrets.yaml)
#   2. render patches from cluster.env
#   3. generate and validate machine configs
#   4. apply configs to nodes booted from the Talos ISO (maintenance mode)
#   5. bootstrap etcd on the first control-plane node, fetch kubeconfig
#   6. install Cilium from Harbor (nodes stay NotReady until a CNI runs)
#   7. taint GPU nodes, wait for the cluster to report healthy
# =============================================================================
set -Eeuo pipefail

ENV_FILE=${1:?usage: bootstrap.sh <cluster.env> [render]}
MODE=${2:-full}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT="$HERE/out"

# shellcheck source=/dev/null
source "$ENV_FILE"
for v in CLUSTER_NAME CLUSTER_VIP CLUSTER_ENDPOINT KUBERNETES_VERSION TALOS_VERSION CONTROL_PLANE_IPS \
         GPU_WORKER_IPS VIP_INTERFACE INSTALL_DISK_SELECTOR NTP_SERVER DNS_SERVER HARBOR_HOST \
         HARBOR_PULL_USER HARBOR_PULL_PASSWORD_FILE ENCLAVE_CA_FILE SCHEMATIC_CONTROL_PLANE SCHEMATIC_GPU; do
  [ -n "${!v:-}" ] || { echo "cluster.env is missing $v" >&2; exit 1; }
done
for bin in talosctl envsubst; do
  command -v "$bin" >/dev/null || { echo "$bin not found" >&2; exit 1; }
done

mkdir -p "$OUT/patches"
chmod 700 "$OUT"

# --- 1. secrets --------------------------------------------------------------
if [ ! -f "$OUT/secrets.yaml" ]; then
  talosctl gen secrets -o "$OUT/secrets.yaml"
  chmod 600 "$OUT/secrets.yaml"
  echo "generated cluster secrets: back up $OUT/secrets.yaml offline NOW"
fi

# --- 2. render patches -------------------------------------------------------
HARBOR_PULL_PASSWORD=$(<"$HARBOR_PULL_PASSWORD_FILE")
# The CA is embedded as a YAML block scalar, so indent every line by two spaces.
ENCLAVE_CA_INDENTED=$(sed 's/^/  /' "$ENCLAVE_CA_FILE")
export CLUSTER_VIP VIP_INTERFACE INSTALL_DISK_SELECTOR NTP_SERVER DNS_SERVER HARBOR_HOST \
       HARBOR_PULL_USER HARBOR_PULL_PASSWORD ENCLAVE_CA_INDENTED SCHEMATIC_CONTROL_PLANE SCHEMATIC_GPU TALOS_VERSION
# shellcheck disable=SC2016 # a literal variable list for envsubst, not an expansion
VARS='${CLUSTER_VIP} ${VIP_INTERFACE} ${INSTALL_DISK_SELECTOR} ${NTP_SERVER} ${DNS_SERVER} ${HARBOR_HOST}
      ${HARBOR_PULL_USER} ${HARBOR_PULL_PASSWORD} ${ENCLAVE_CA_INDENTED} ${SCHEMATIC_CONTROL_PLANE}
      ${SCHEMATIC_GPU} ${TALOS_VERSION}'
for p in common controlplane gpu-worker; do
  envsubst "$VARS" < "$HERE/patches/$p.yaml" > "$OUT/patches/$p.yaml"
done
chmod 600 "$OUT"/patches/*.yaml

# --- 3. generate and validate ------------------------------------------------
# --with-cluster-discovery=false: the default discovery service is a public
# endpoint (discovery.talos.dev) that the enclave can never reach.
talosctl gen config "$CLUSTER_NAME" "$CLUSTER_ENDPOINT" \
  --with-secrets "$OUT/secrets.yaml" \
  --kubernetes-version "$KUBERNETES_VERSION" \
  --with-cluster-discovery=false \
  --with-docs=false --with-examples=false \
  --config-patch @"$OUT/patches/common.yaml" \
  --config-patch-control-plane @"$OUT/patches/controlplane.yaml" \
  --config-patch-worker @"$OUT/patches/gpu-worker.yaml" \
  --output "$OUT" --force
talosctl validate --config "$OUT/controlplane.yaml" --mode metal
talosctl validate --config "$OUT/worker.yaml" --mode metal
chmod 600 "$OUT/controlplane.yaml" "$OUT/worker.yaml" "$OUT/talosconfig"

[ "$MODE" = "render" ] && { echo "rendered configs in $OUT"; exit 0; }

# --- 4. apply to nodes in maintenance mode -----------------------------------
read -r -a CP <<< "$CONTROL_PLANE_IPS"
read -r -a GPU <<< "$GPU_WORKER_IPS"
for ip in "${CP[@]}"; do
  talosctl apply-config --insecure --nodes "$ip" --file "$OUT/controlplane.yaml"
done
for ip in "${GPU[@]}"; do
  talosctl apply-config --insecure --nodes "$ip" --file "$OUT/worker.yaml"
done

export TALOSCONFIG="$OUT/talosconfig"
talosctl config endpoint "${CP[@]}"
talosctl config node "${CP[0]}"

# --- 5. bootstrap etcd once, on one node --------------------------------------
echo "waiting for ${CP[0]} to finish installing and reboot"
until talosctl --nodes "${CP[0]}" version >/dev/null 2>&1; do sleep 10; done
talosctl bootstrap --nodes "${CP[0]}"
talosctl kubeconfig "$OUT/kubeconfig" --nodes "${CP[0]}" --force
export KUBECONFIG="$OUT/kubeconfig"
until kubectl get --raw /readyz >/dev/null 2>&1; do sleep 10; done

# --- 6. CNI ------------------------------------------------------------------
kubectl apply -f "$REPO/platform/base/namespaces.yaml"
helm upgrade --install cilium "oci://$HARBOR_HOST/charts/cilium" --version 1.20.3 \
  --namespace kube-system --values "$REPO/platform/cilium/values.yaml" --wait --timeout 15m
kubectl apply -f "$REPO/platform/cilium/enclave-egress-baseline.yaml"

# --- 7. GPU taints and health -------------------------------------------------
for ip in "${GPU[@]}"; do
  node=$(kubectl get nodes -o jsonpath="{.items[?(@.status.addresses[0].address=='$ip')].metadata.name}")
  kubectl taint nodes "$node" nvidia.com/gpu=present:NoSchedule --overwrite
done
talosctl health --nodes "${CP[0]}" --wait-timeout 20m
kubectl get nodes -o wide

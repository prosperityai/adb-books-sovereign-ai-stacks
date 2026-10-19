#!/usr/bin/env bash
# =============================================================================
# upgrade_cluster.sh — offline Talos and Kubernetes upgrades, one node at a
# time, with a health gate between every step
#
# Book:    Sovereign AI Stacks, Chapter 12
# Runs on: the enclave admin workstation (talosctl matching the TARGET Talos
#          version, kubectl 1.37, oras 1.3.x, python3)
#
# Usage:
#   upgrade_cluster.sh <cluster.env> preflight talos <vX.Y.Z>
#   upgrade_cluster.sh <cluster.env> talos <vX.Y.Z>       # e.g. v1.14.4
#   upgrade_cluster.sh <cluster.env> preflight k8s <X.Y.Z>
#   upgrade_cluster.sh <cluster.env> k8s <X.Y.Z>          # e.g. 1.37.1
#
# cluster.env is the same file infra/talos/bootstrap.sh used (Chapter 4): it
# names the nodes, the Harbor host and the two Image Factory schematics.
#
# Every image the upgrade needs must already be in Harbor, imported through
# the Chapter 3 bundle. Talos nodes pull factory.talos.dev/... and
# registry.k8s.io/... by their upstream names; the registry mirror config
# from Chapter 4 serves them from harbor/mirror/... and never falls back to
# the internet. Preflight proves every one of them resolves BEFORE any node
# is touched, because a node that reboots into a missing image does not
# come back by itself.
# =============================================================================
set -Eeuo pipefail

usage() { sed -n '/^# Usage:/,/^#   upgrade_cluster.sh <cluster.env> k8s/p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 3 ] || usage
ENV_FILE=$1
shift
PREFLIGHT_ONLY=0
if [ "$1" = "preflight" ]; then PREFLIGHT_ONLY=1; shift; fi
[ $# -eq 2 ] || usage
KIND=$1 TARGET=$2
case "$KIND" in
  talos) [[ "$TARGET" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Talos version looks like v1.14.4" >&2; exit 2; } ;;
  k8s)   [[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Kubernetes version looks like 1.37.1" >&2; exit 2; } ;;
  *) usage ;;
esac

# shellcheck source=/dev/null
source "$ENV_FILE"
for v in CONTROL_PLANE_IPS GPU_WORKER_IPS HARBOR_HOST SCHEMATIC_CONTROL_PLANE SCHEMATIC_GPU; do
  [ -n "${!v:-}" ] || { echo "cluster.env is missing $v" >&2; exit 1; }
done
for bin in talosctl kubectl oras python3; do
  command -v "$bin" >/dev/null || { echo "$bin not found" >&2; exit 1; }
done
HERE=$(cd "$(dirname "$0")" && pwd)
TALOS_DIR=$(cd "$HERE/../../infra/talos" && pwd)
export TALOSCONFIG=${TALOSCONFIG:-$TALOS_DIR/out/talosconfig}
export KUBECONFIG=${KUBECONFIG:-$TALOS_DIR/out/kubeconfig}
read -r -a CP <<< "$CONTROL_PLANE_IPS"
read -r -a GPU <<< "$GPU_WORKER_IPS"
MAX_BACKUP_AGE=${MAX_BACKUP_AGE:-86400}

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "ABORT: $*" >&2; exit 1; }
ceph() { kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph "$@"; }
node_name() {  # node_name <ip>
  kubectl get nodes -o jsonpath="{.items[?(@.status.addresses[0].address=='$1')].metadata.name}"
}
# Map an upstream image to its path in Harbor's mirror project (Chapter 3:
# the registry host is dropped, the rest of the path is kept).
harbor_path() { echo "$HARBOR_HOST/mirror/${1#*/}"; }

# --- health gates -----------------------------------------------------------------
# Ceph is healthy when no check fires except the noout flag we set ourselves.
ceph_ok() {
  ceph health -f json | python3 -c '
import json, sys
checks = set(json.load(sys.stdin).get("checks", {})) - {"OSDMAP_FLAGS"}
sys.exit(1 if checks else 0)'
}
wait_ceph() {
  local deadline=$(( $(date +%s) + 1800 ))
  until ceph_ok; do
    [ "$(date +%s)" -lt "$deadline" ] || die "Ceph not healthy after 30 minutes: $(ceph health 2>&1)"
    sleep 20
  done
}
all_nodes_ready() {
  [ -z "$(kubectl get nodes --no-headers | awk '$2 != "Ready"')" ]
}
etcd_ok() {
  local members
  members=$(talosctl -n "${CP[0]}" etcd members 2>/dev/null | tail -n +2 | wc -l)
  [ "$members" -eq "${#CP[@]}" ] && talosctl -n "${CP[0]}" etcd status >/dev/null
}
recent_backups() {
  local ts age
  ts=$(kubectl -n velero get backups -l velero.io/schedule-name=platform-resources \
    -o jsonpath='{range .items[?(@.status.phase=="Completed")]}{.status.completionTimestamp}{"\n"}{end}' | sort | tail -1)
  [ -n "$ts" ] || die "no completed platform-resources backup: take one before upgrading"
  age=$(( $(date +%s) - $(date -d "$ts" +%s) ))
  [ "$age" -le "$MAX_BACKUP_AGE" ] || die "newest platform backup is $((age / 3600)) h old; run one first"
  for db in ai-gateway/litellm-db identity/keycloak-db ai-gateway/audit-db; do
    ts=$(kubectl -n "${db%/*}" get backups.postgresql.cnpg.io -l cnpg.io/cluster="${db#*/}" \
      -o jsonpath='{range .items[?(@.status.phase=="completed")]}{.status.stoppedAt}{"\n"}{end}' | sort | tail -1)
    [ -n "$ts" ] || die "no completed base backup for ${db#*/}"
    age=$(( $(date +%s) - $(date -d "$ts" +%s) ))
    [ "$age" -le "$MAX_BACKUP_AGE" ] || die "${db#*/} base backup is $((age / 3600)) h old"
  done
}
in_harbor() {
  oras manifest fetch --descriptor "$(harbor_path "$1")" >/dev/null 2>&1
}

# --- preflight ------------------------------------------------------------------------
preflight() {
  local missing=0 client img
  log "preflight: $KIND -> $TARGET"
  all_nodes_ready || die "not every node is Ready"
  etcd_ok || die "etcd does not have ${#CP[@]} healthy members"
  ceph_ok || die "Ceph is not healthy: $(ceph health 2>&1)"
  recent_backups
  client=$(talosctl version --client --short 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [ "$KIND" = "talos" ]; then
    [ "$client" = "$TARGET" ] || die "talosctl is $client; use the $TARGET client for a $TARGET upgrade"
    for img in "factory.talos.dev/metal-installer/$SCHEMATIC_CONTROL_PLANE:$TARGET" \
               "factory.talos.dev/metal-installer/$SCHEMATIC_GPU:$TARGET"; do
      if in_harbor "$img"; then log "  in Harbor: $img"; else log "  MISSING:   $img"; missing=1; fi
    done
  else
    # The exact list talosctl will pre-pull: control-plane images, kubelet, etcd, CoreDNS.
    while read -r img; do
      [ -n "$img" ] || continue
      if in_harbor "$img"; then log "  in Harbor: $img"; else log "  MISSING:   $img"; missing=1; fi
    done < <(talosctl images k8s-bundle --k8s-version "v$TARGET")
    talosctl -n "${CP[0]}" upgrade-k8s --to "$TARGET" --dry-run
  fi
  [ "$missing" -eq 0 ] || die "images missing from Harbor; add them to the Zarf bundle and import it (Chapter 3)"
  log "preflight passed"
}

# --- Talos: one node at a time ---------------------------------------------------------------
NOOUT=0
# shellcheck disable=SC2329 # invoked by the EXIT trap
on_exit() {
  if [ "$NOOUT" = "1" ]; then
    echo "WARNING: Ceph 'noout' is still set. Once the node is back and Ceph is healthy:" >&2
    echo "  kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd unset noout" >&2
  fi
}
trap on_exit EXIT

upgrade_node() {  # upgrade_node <ip> <schematic> <role>
  local ip=$1 schematic=$2 role=$3 name image gpus
  name=$(node_name "$ip")
  image="factory.talos.dev/metal-installer/$schematic:$TARGET"
  if kubectl get node "$name" -o jsonpath='{.status.nodeInfo.osImage}' | grep -q "($TARGET)"; then
    log "$name ($ip) already on $TARGET, skipping"; return
  fi
  log "upgrading $role $name ($ip) to $image"
  # Control-plane nodes carry the Ceph OSDs (Chapter 4). Without noout, Ceph
  # starts re-replicating a node's data 10 minutes after it goes down, then
  # moves it all back when it returns.
  if [ "$role" = "control-plane" ]; then ceph osd set noout; NOOUT=1; fi
  # Talos cordons and drains the node itself, and refuses a control-plane
  # upgrade that would lose etcd quorum.
  talosctl -n "$ip" upgrade --image "$image" --wait
  kubectl wait --for=condition=Ready "node/$name" --timeout=20m
  kubectl get node "$name" -o jsonpath='{.status.nodeInfo.osImage}' | grep -q "($TARGET)" \
    || die "$name reports $(kubectl get node "$name" -o jsonpath='{.status.nodeInfo.osImage}'), not $TARGET"
  kubectl uncordon "$name"
  if [ "$role" = "control-plane" ]; then
    etcd_ok || die "etcd unhealthy after $name"
    wait_ceph
    ceph osd unset noout; NOOUT=0
  fi
  if [ "$role" = "gpu" ]; then
    # The NVIDIA modules are system extensions in the installer image; a
    # wrong schematic boots fine and shows zero GPUs.
    local deadline=$(( $(date +%s) + 900 ))
    until gpus=$(kubectl get node "$name" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'); [ "${gpus:-0}" -gt 0 ]; do
      [ "$(date +%s)" -lt "$deadline" ] || die "$name came back with no allocatable GPUs; check talosctl -n $ip get extensions"
      sleep 15
    done
    log "$name: $gpus GPUs allocatable"
  fi
  wait_ceph
  talosctl -n "${CP[0]}" health --wait-timeout 10m >/dev/null
  log "$name done"
}

upgrade_talos() {
  local ip
  for ip in "${CP[@]}"; do upgrade_node "$ip" "$SCHEMATIC_CONTROL_PLANE" control-plane; done
  for ip in "${GPU[@]}"; do upgrade_node "$ip" "$SCHEMATIC_GPU" gpu; done
  log "all nodes on $TARGET"
  kubectl get nodes -o custom-columns='NODE:.metadata.name,OS:.status.nodeInfo.osImage,KUBELET:.status.nodeInfo.kubeletVersion'
}

# --- Kubernetes: talosctl walks the components in order and can be re-run -------------------
upgrade_k8s() {
  log "upgrading Kubernetes to $TARGET"
  # Pre-pulls every image first, then API server, controller-manager and
  # scheduler node by node, then kubelets, then the bootstrap manifests.
  # If it stops part-way, fix the cause and run the same command again.
  talosctl -n "${CP[0]}" upgrade-k8s --to "$TARGET"
  all_nodes_ready || die "a node is NotReady after the upgrade"
  local bad
  bad=$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.nodeInfo.kubeletVersion}{"\n"}{end}' \
    | awk -v want="v$TARGET" '$2 != want')
  [ -z "$bad" ] || die "kubelets not on v$TARGET: $bad"
  wait_ceph
  talosctl -n "${CP[0]}" health --wait-timeout 10m >/dev/null
  kubectl version
}

preflight
[ "$PREFLIGHT_ONLY" = "1" ] && exit 0
case "$KIND" in
  talos) upgrade_talos ;;
  k8s) upgrade_k8s ;;
esac
log "upgrade complete. Update TALOS_VERSION / KUBERNETES_VERSION in cluster.env to match."

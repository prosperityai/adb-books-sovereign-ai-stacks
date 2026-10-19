#!/usr/bin/env bash
# =============================================================================
# barman_plugin_tls.sh — create the mTLS Secrets the Barman Cloud plugin needs,
# without cert-manager
#
# Book:    Sovereign AI Stacks, Chapter 12
# Runs on: the enclave admin workstation (openssl 3.x, kubectl)
#
# Usage:   ./barman_plugin_tls.sh [days]        # default 730 days
#
# The CloudNativePG operator talks to the plugin over gRPC with mutual TLS.
# The chart normally asks cert-manager for a self-signed server certificate
# (CN/SAN "barman-cloud", the Service name) and a self-signed client
# certificate. This script produces the same two Secrets with openssl, so
# the operator's trust model is unchanged:
#   barman-cloud-server-tls   tls.crt, tls.key, ca.crt (= tls.crt)
#   barman-cloud-client-tls   tls.crt, tls.key, ca.crt (= tls.crt)
#
# cert-manager would renew these. Nothing renews them here: the expiry date
# goes into the handover checklist, and re-running this script followed by
# `kubectl -n cnpg-system rollout restart deploy/plugin-barman-cloud deploy/cloudnative-pg`
# rotates them. When they expire, WAL archiving stops on every cluster.
# =============================================================================
set -Eeuo pipefail

usage() { echo "usage: barman_plugin_tls.sh [validity-days]" >&2; exit 2; }
[ $# -le 1 ] || usage
DAYS=${1:-730}
[[ "$DAYS" =~ ^[0-9]+$ ]] || usage
NS=cnpg-system

for bin in openssl kubectl; do
  command -v "$bin" >/dev/null || { echo "$bin not found" >&2; exit 1; }
done

WORK=$(mktemp -d)
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

make_cert() {  # <name> <cn> <extendedKeyUsage> [subjectAltName]
  local name=$1 cn=$2 eku=$3 san=${4:-}
  local ext=(-addext "keyUsage=critical,digitalSignature,keyEncipherment" -addext "extendedKeyUsage=$eku")
  [ -n "$san" ] && ext+=(-addext "subjectAltName=$san")
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -sha256 \
    -days "$DAYS" -subj "/CN=$cn" "${ext[@]}" \
    -keyout "$WORK/$name.key" -out "$WORK/$name.crt" 2>/dev/null
  kubectl -n "$NS" create secret generic "barman-cloud-$name-tls" --type=kubernetes.io/tls \
    --from-file=tls.crt="$WORK/$name.crt" --from-file=tls.key="$WORK/$name.key" \
    --from-file=ca.crt="$WORK/$name.crt" --dry-run=client -o yaml | kubectl apply -f -
  echo "barman-cloud-$name-tls expires $(openssl x509 -in "$WORK/$name.crt" -noout -enddate | cut -d= -f2)"
}

make_cert server barman-cloud serverAuth "DNS:barman-cloud,DNS:barman-cloud.$NS.svc"
make_cert client barman-cloud-client clientAuth

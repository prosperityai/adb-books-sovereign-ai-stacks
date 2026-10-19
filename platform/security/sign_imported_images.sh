#!/usr/bin/env bash
# =============================================================================
# sign_imported_images.sh — sign every image that passed the import gate
#
# Book:    Sovereign AI Stacks, Chapter 10 (threats T3, T12)
# Runs on: the ENCLAVE import station, after verify_bundle.sh (Chapter 3)
#          succeeded and `zarf package mirror-resources` pushed the images
# Needs:   cosign 2.6.x, oras 1.3.x, GNU coreutils
#
# Usage:   COSIGN_PASSWORD=... ./sign_imported_images.sh <images.txt> <enclave.key> [record.jsonl]
#
#   images.txt   one image per line, upstream names as the charts use them
#                (e.g. the output of `zarf package inspect images`); blank
#                lines and # comments are ignored
#   enclave.key  the enclave ADMISSION key. Its public half is the one in
#                ConfigMap kyverno/enclave-cosign-pub. It is not the Chapter 3
#                transfer key: that one says "this came from our transfer
#                station"; this one says "this digest passed the enclave gate".
#   record.jsonl appended with one JSON line per signed digest (evidence pack)
#
# Environment: HARBOR (default harbor.enclave.internal), ENCLAVE_CA (CA file
# for Harbor's certificate, optional if the system trust store has it).
# Exit codes: 0 all signed; 1 usage or any failure (nothing is skipped).
# =============================================================================
set -Eeuo pipefail

usage() {
  echo "usage: COSIGN_PASSWORD=... $0 <images.txt> <enclave.key> [record.jsonl]" >&2
  exit 1
}

[ $# -ge 2 ] && [ $# -le 3 ] || usage
IMAGES=$1
KEY=$2
RECORD=${3:-signing-record.jsonl}
HARBOR=${HARBOR:-harbor.enclave.internal}

[ -f "$IMAGES" ] || { echo "no such file: $IMAGES" >&2; usage; }
[ -f "$KEY" ] || { echo "no such key: $KEY" >&2; usage; }
: "${COSIGN_PASSWORD:?set COSIGN_PASSWORD for the enclave key}"
command -v cosign >/dev/null || { echo "cosign not found" >&2; exit 1; }
command -v oras >/dev/null || { echo "oras not found" >&2; exit 1; }

oras_args=()
if [ -n "${ENCLAVE_CA:-}" ]; then
  oras_args+=(--ca-file "$ENCLAVE_CA")
fi

# The same rewrite Kyverno's route-through-harbor rule applies: drop the
# upstream registry host, keep the path, prefix the Harbor mirror project.
mirror_ref() {
  local ref=$1 first=${1%%/*}
  case "$ref" in
    "$HARBOR"/*) echo "$ref"; return ;;
  esac
  if [ "$first" != "$ref" ] && { [[ $first == *.* ]] || [[ $first == *:* ]] || [ "$first" = localhost ]; }; then
    ref=${ref#*/}
  fi
  echo "$HARBOR/mirror/$ref"
}

# Fingerprint of the public half: ties every signature in the record to the
# exact key Kyverno trusts.
KEY_FPR=$(cosign public-key --key "$KEY" 2>/dev/null | sha256sum | cut -d' ' -f1)
SIGNER="$(id -un)@$(hostname)"
count=0

while IFS= read -r line || [ -n "$line" ]; do
  image=${line%%#*}
  image=${image//[[:space:]]/}
  [ -n "$image" ] || continue

  target=$(mirror_ref "$image")
  repo=${target%@*}
  if [[ $target == *@sha256:* ]]; then
    digest=${target#*@}
  else
    digest=$(oras resolve "${oras_args[@]}" "$target")
  fi
  # Strip a tag from the repository part: we sign the digest, never a tag.
  last=${repo##*/}
  if [[ $last == *:* ]]; then
    repo=${repo%:*}
  fi

  cosign sign --yes --key "$KEY" --tlog-upload=false "$repo@$digest" >/dev/null 2>&1 \
    || { echo "SIGN FAILED: $repo@$digest" >&2; exit 1; }

  printf '{"image":"%s","harbor_ref":"%s@%s","signed_at":"%s","signer":"%s","key_sha256":"%s"}\n' \
    "$image" "$repo" "$digest" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SIGNER" "$KEY_FPR" >> "$RECORD"
  echo "signed: $repo@$digest"
  count=$((count + 1))
done < "$IMAGES"

echo "SIGNED: $count image(s), key sha256 $KEY_FPR, record $RECORD"

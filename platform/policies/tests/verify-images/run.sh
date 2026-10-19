#!/usr/bin/env bash
# =============================================================================
# run.sh — test verify-images.yaml against real signatures in a lab registry
#
# Book:  Sovereign AI Stacks, Chapter 10
# Needs: kyverno CLI 1.19.x, cosign 2.6.x, oras 1.3.x, crane 0.20.x, and a
#        registry that answers as https://harbor.enclave.internal with a
#        certificate this machine trusts (Harbor itself, or the lab registry
#        from the chapter's hands-on lab)
#
# Usage: platform/policies/tests/verify-images/run.sh [--keep]
#   --keep   leave the working directory (keys, values.yaml) for inspection
#
# Pushes three copies of a one-file image, signs one with a fresh "enclave"
# key and one with a "rogue" key, then runs the Kyverno CLI test. The keys are
# throwaway lab keys made in a temporary directory, never the real ones.
# Exit codes: 0 every expectation met; 1 any failure.
# =============================================================================
set -Eeuo pipefail

usage() { echo "usage: $0 [--keep]" >&2; exit 1; }
KEEP=0
case "${1:-}" in
  "") ;;
  --keep) KEEP=1 ;;
  *) usage ;;
esac
[ $# -le 1 ] || usage

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../../.." && pwd)
REGISTRY=harbor.enclave.internal
for tool in kyverno cosign oras crane; do
  command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 1; }
done

WORK=$(mktemp -d)
cleanup() { if [ "$KEEP" = 1 ]; then echo "kept: $WORK"; else rm -rf "$WORK"; fi; }
trap cleanup EXIT

# Same relative layout as the repo, so ../../verify-images.yaml resolves.
mkdir -p "$WORK/policies/tests/verify-images" "$WORK/keys"
cp "$REPO/platform/policies/verify-images.yaml" "$WORK/policies/"
cp "$HERE"/*.yaml "$WORK/policies/tests/verify-images/"

export COSIGN_PASSWORD=lab-only
( cd "$WORK/keys"
  cosign generate-key-pair --output-key-prefix enclave >/dev/null 2>&1
  cosign generate-key-pair --output-key-prefix rogue >/dev/null 2>&1 )

# One tiny image, pushed under three names. Same digest, different signatures.
echo "verify-images lab $(date -u +%s)" > "$WORK/hello.txt"
tar -C "$WORK" -cf "$WORK/layer.tar" hello.txt
for name in signed unsigned rogue; do
  crane append -f "$WORK/layer.tar" -t "$REGISTRY/mirror/library/demo-$name:1.0" >/dev/null 2>&1
done

# Sign with the real signing script (enclave key) and directly (rogue key).
echo "docker.io/library/demo-signed:1.0" > "$WORK/images.txt"
"$REPO/platform/security/sign_imported_images.sh" "$WORK/images.txt" "$WORK/keys/enclave.key" \
  "$WORK/signing-record.jsonl" >/dev/null
rogue_digest=$(crane digest "$REGISTRY/mirror/library/demo-rogue:1.0")
cosign sign --yes --key "$WORK/keys/rogue.key" --tlog-upload=false \
  "$REGISTRY/mirror/library/demo-rogue@$rogue_digest" >/dev/null 2>&1

# The ConfigMap value Kyverno would read, as a CLI variables file.
{
  printf 'apiVersion: cli.kyverno.io/v1alpha1\nkind: Values\npolicies:\n'
  printf '  - name: verify-images\n    rules:\n      - name: verify-enclave-signature\n'
  printf '        values:\n          enclavekey.data.pubkey: |\n'
  sed 's/^/            /' "$WORK/keys/enclave.pub"
} > "$WORK/policies/tests/verify-images/values.yaml"

echo "== kyverno test"
kyverno test --remove-color --file-name verify-images-test.yaml "$WORK/policies/tests/verify-images"

echo "== unsigned init container"
out=$(kyverno apply "$WORK/policies/verify-images.yaml" \
  --resource "$WORK/policies/tests/verify-images/resources-init.yaml" \
  --values-file "$WORK/policies/tests/verify-images/values.yaml" --remove-color 2>&1 || true)
if grep -q "unverified image $REGISTRY/mirror/library/demo-unsigned" <<<"$out"; then
  echo "PASS: bad-unsigned-init rejected (init container image unsigned)"
else
  echo "$out" >&2
  echo "FAIL: bad-unsigned-init was not rejected" >&2
  exit 1
fi

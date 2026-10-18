#!/usr/bin/env bash
# =============================================================================
# verify_bundle.sh — verify and unpack a transfer bundle inside the enclave
#
# Book:    Sovereign AI Stacks, Chapter 3
# Runs on: the ENCLAVE import station
# Needs:   cosign 2.6.x, GNU coreutils, tar
#
# Usage:   ./verify_bundle.sh <media-dir> <restore-dir> <cosign.pub>
#
# The public key must already be inside the enclave, installed during the key
# ceremony. Never trust a public key that arrives on the same media as the
# bundle it verifies: an attacker who can swap the bundle can swap the key.
#
# Exit codes: 0 verified and unpacked; 1 any verification failure.
# =============================================================================
set -Eeuo pipefail

MEDIA=${1:?usage: verify_bundle.sh <media-dir> <restore-dir> <cosign.pub>}
RESTORE=${2:?usage: verify_bundle.sh <media-dir> <restore-dir> <cosign.pub>}
PUBKEY=${3:?usage: verify_bundle.sh <media-dir> <restore-dir> <cosign.pub>}

fail() { echo "VERIFY FAILED: $*" >&2; exit 1; }

[ -f "$MEDIA/PARTS.sha256" ] || fail "missing $MEDIA/PARTS.sha256"
[ -f "$PUBKEY" ] || fail "missing public key $PUBKEY"
[ ! -e "$RESTORE" ] || [ -z "$(ls -A "$RESTORE")" ] || fail "$RESTORE is not empty"

# 1. Per-part checksums first: find a corrupt USB part before spending an hour
#    reassembling 300 GB.
echo "== step 1/4: part checksums"
( cd "$MEDIA" && sha256sum --quiet -c PARTS.sha256 ) || fail "a media part is corrupt; re-copy the parts listed above"

# 2. Reassemble. Parts sort correctly because split used fixed-width suffixes.
echo "== step 2/4: reassemble"
mkdir -p "$RESTORE"
cat "$MEDIA"/bundle.tar.part-* | tar -C "$RESTORE" -xf -

# 3. Signature over the checksum list.
echo "== step 3/4: signature"
cosign verify-blob --key "$PUBKEY" --insecure-ignore-tlog=true \
  --signature "$RESTORE/SHA256SUMS.sig" "$RESTORE/SHA256SUMS" >/dev/null 2>&1 \
  || fail "SHA256SUMS signature does not verify against $PUBKEY"

# 4. Every file against the signed list, then every model against its own manifest.
echo "== step 4/4: file checksums"
( cd "$RESTORE" && sha256sum --quiet -c SHA256SUMS ) || fail "file checksum mismatch"
if [ -d "$RESTORE/models" ]; then
  for manifest in "$RESTORE"/models/*/MANIFEST.sha256; do
    [ -f "$manifest" ] || continue
    ( cd "$(dirname "$manifest")" && sha256sum --quiet -c MANIFEST.sha256 ) \
      || fail "model manifest mismatch in $(dirname "$manifest")"
  done
fi

echo "VERIFIED: $(wc -l < "$RESTORE/SHA256SUMS") files, signature OK, unpacked to $RESTORE"

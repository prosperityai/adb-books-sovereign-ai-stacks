#!/usr/bin/env bash
# =============================================================================
# sign_and_sbom.sh — seal a transfer directory before it crosses the air gap
#
# Book:    Sovereign AI Stacks, Chapter 3
# Runs on: the CONNECTED transfer station
# Needs:   cosign 2.6.x, syft 1.54.x, GNU coreutils
#
# Usage:   COSIGN_PASSWORD=... ./sign_and_sbom.sh <transfer-dir> <cosign.key>
#
# Produces, inside <transfer-dir>:
#   sbom/*.spdx.json   SBOMs for the Python wheelhouse and every tool binary
#   SHA256SUMS         hash of every file in the bundle
#   SHA256SUMS.sig     cosign signature over SHA256SUMS
# One signature over one checksum file covers the whole bundle: verify the
# signature, then verify every hash against it.
# =============================================================================
set -Eeuo pipefail

TRANSFER=${1:?usage: sign_and_sbom.sh <transfer-dir> <cosign.key>}
KEY=${2:?usage: sign_and_sbom.sh <transfer-dir> <cosign.key>}

[ -d "$TRANSFER" ] || { echo "no such directory: $TRANSFER" >&2; exit 1; }
[ -f "$KEY" ] || { echo "no such key: $KEY" >&2; exit 1; }
KEY=$(realpath "$KEY")   # we cd below; keep the key path valid
: "${COSIGN_PASSWORD:?set COSIGN_PASSWORD for the signing key}"
command -v cosign >/dev/null || { echo "cosign not found" >&2; exit 1; }
command -v syft >/dev/null || { echo "syft not found" >&2; exit 1; }

cd "$TRANSFER"
mkdir -p sbom

# 1. SBOMs. Zarf already embeds image SBOMs in its package; here we cover the
#    things Zarf does not see: Python wheels and the CLI binaries we carry in.
if [ -d wheelhouse ]; then
  syft scan "dir:wheelhouse" -o "spdx-json=sbom/wheelhouse.spdx.json" -q
  echo "sbom: wheelhouse"
fi
if [ -d bin ]; then
  for tool in bin/*; do
    [ -f "$tool" ] || continue
    syft scan "file:$tool" -o "spdx-json=sbom/$(basename "$tool").spdx.json" -q
    echo "sbom: $tool"
  done
fi

# 2. Checksums over every file except the checksum file and its signature.
#    LC_ALL=C sort gives byte-order sorting, so the file is reproducible.
find . -type f ! -name 'SHA256SUMS' ! -name 'SHA256SUMS.sig' -print0 \
  | LC_ALL=C sort -z \
  | xargs -0 sha256sum > SHA256SUMS
echo "checksums: $(wc -l < SHA256SUMS) files"

# 3. Sign the checksum file. --tlog-upload=false: the public transparency log
#    is an internet service, and the enclave could never check it anyway.
cosign sign-blob --yes --key "$KEY" --tlog-upload=false \
  --output-signature SHA256SUMS.sig SHA256SUMS >/dev/null
echo "signed: SHA256SUMS.sig"

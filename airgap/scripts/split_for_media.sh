#!/usr/bin/env bash
# =============================================================================
# split_for_media.sh — cut a sealed transfer directory into media-sized parts
#
# Book:    Sovereign AI Stacks, Chapter 3
# Runs on: the CONNECTED transfer station, after sign_and_sbom.sh
#
# Usage:   ./split_for_media.sh <transfer-dir> <media-dir> [part-size]
#          part-size defaults to 3900M, which fits FAT32/exFAT-safe 4 GB limits
#          and keeps a single bad part cheap to re-copy.
#
# Produces in <media-dir>:
#   bundle.tar.part-0000 ... bundle.tar.part-NNNN
#   PARTS.sha256   per-part checksums, verified BEFORE reassembly
# =============================================================================
set -Eeuo pipefail

TRANSFER=${1:?usage: split_for_media.sh <transfer-dir> <media-dir> [part-size]}
MEDIA=${2:?usage: split_for_media.sh <transfer-dir> <media-dir> [part-size]}
PART_SIZE=${3:-3900M}

[ -f "$TRANSFER/SHA256SUMS.sig" ] || { echo "$TRANSFER is not sealed; run sign_and_sbom.sh first" >&2; exit 1; }
mkdir -p "$MEDIA"

# Plain tar, no compression: model weights and zstd-compressed Zarf packages
# do not compress further, and gzip would cost hours of CPU on 300 GB.
tar -C "$TRANSFER" -cf - . \
  | split -b "$PART_SIZE" -d -a 4 - "$MEDIA/bundle.tar.part-"

( cd "$MEDIA" && sha256sum bundle.tar.part-* > PARTS.sha256 )
echo "parts: $(wc -l < "$MEDIA/PARTS.sha256")  size: $(du -sh "$MEDIA" | cut -f1)"

#!/usr/bin/env bash
# =============================================================================
# push_model_oci.sh — store a verified model in Harbor as an OCI artifact
#
# Book:    Sovereign AI Stacks, Chapter 3
# Runs on: the ENCLAVE import station, after verify_bundle.sh
# Needs:   oras 1.3.x, python3 (for reading model-info.json)
#
# Usage:   ./push_model_oci.sh <model-dir> <registry>/<project>
#   e.g.   ./push_model_oci.sh /import/models/qwen3.8-27b-fp8 harbor.enclave.internal/models
#
# Log in first:  oras login harbor.enclave.internal -u 'robot$models-push' --password-stdin
# Lab only:       PLAIN_HTTP=1 for a throwaway registry without TLS. Harbor in
#                 the enclave always serves TLS from the enclave CA.
#
# The artifact is tagged with the first 12 characters of the upstream commit
# SHA, so the tag itself tells you exactly which weights you are running.
# The digest is written to <model-dir>/oci-digest.txt for the deployment
# manifests in Chapter 5, which pin by digest.
# =============================================================================
set -Eeuo pipefail

MODEL_DIR=${1:?usage: push_model_oci.sh <model-dir> <registry>/<project>}
TARGET=${2:?usage: push_model_oci.sh <model-dir> <registry>/<project>}

[ -f "$MODEL_DIR/MANIFEST.sha256" ] || { echo "missing MANIFEST.sha256 in $MODEL_DIR" >&2; exit 1; }
[ -f "$MODEL_DIR/model-info.json" ] || { echo "missing model-info.json in $MODEL_DIR" >&2; exit 1; }

read_info() { python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$MODEL_DIR/model-info.json" "$1"; }
NAME=$(read_info name)
REPO=$(read_info repo)
REVISION=$(read_info revision)
LICENCE=$(read_info licence)
TAG=${REVISION:0:12}
REF="$TARGET/$NAME:$TAG"

cd "$MODEL_DIR"

# Every file in the manifest becomes one layer, with its relative path as the
# layer title, so `oras pull` recreates the exact directory layout. The
# manifest and info files travel with the weights.
files=("MANIFEST.sha256:text/plain" "model-info.json:application/json")
while read -r _ path; do
  files+=("$path:application/octet-stream")
done < MANIFEST.sha256

oras_flags=()
[ "${PLAIN_HTTP:-0}" = "1" ] && oras_flags+=(--plain-http)

echo "pushing $REF (${#files[@]} files)"
oras push "${oras_flags[@]}" "$REF" \
  --artifact-type "application/vnd.sovereign-ai.model.v1" \
  --annotation "org.opencontainers.image.source=https://huggingface.co/$REPO" \
  --annotation "org.opencontainers.image.revision=$REVISION" \
  --annotation "org.opencontainers.image.licenses=$LICENCE" \
  --annotation "org.opencontainers.image.title=$NAME" \
  --format go-template='{{.digest}}' \
  "${files[@]}" > oci-digest.txt

echo "digest: $(cat oci-digest.txt)"
echo "pinned reference: $TARGET/$NAME@$(cat oci-digest.txt)"

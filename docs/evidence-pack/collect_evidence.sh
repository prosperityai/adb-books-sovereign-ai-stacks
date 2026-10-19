#!/usr/bin/env bash
# =============================================================================
# collect_evidence.sh — assemble the auditor evidence pack as one sealed file
#
# Book:    Sovereign AI Stacks, Chapter 10
# Runs on: an enclave admin workstation with read access to the cluster
# Needs:   bash, GNU coreutils, tar, python3; kubectl for cluster evidence;
#          psql for audit-database evidence; cosign 2.6.x to sign the pack
#
# Usage:   docs/evidence-pack/collect_evidence.sh [options]
#   --out DIR             where the tarball goes (default: ./evidence-out)
#   --preflight DIR       per-node preflight JSON from check_host.sh (Ch 1)
#   --import-records DIR  import-record*.json from verify_bundle.sh (Ch 3)
#                         and signing-record*.jsonl from sign_imported_images.sh
#   --dossiers DIR        filled-in model dossiers, *.md (Ch 5)
#   --audit-db CONNINFO   libpq connection string for an audit_auditor login
#   --sign KEY            cosign key: sign SHA256SUMS inside the pack
#   --no-cluster          skip kubectl (offline rehearsal, CI)
#   -h, --help            this text
#
# Output: <out>/evidence-pack-<UTC timestamp>.tar.gz and a .sha256 beside it.
# Inside: SHA256SUMS over every file, manifest.json, and one directory per
# evidence class. Nothing is silently skipped: every gap is listed in
# GAPS.txt, because an auditor should see what is missing, not discover it.
# Exit codes: 0 pack written (check GAPS.txt); 1 usage or a hard failure.
# =============================================================================
set -Eeuo pipefail

usage() { sed -n '/^# Usage:/,/^# Output:/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d' >&2; exit 1; }

OUT=./evidence-out
PREFLIGHT="" IMPORTS="" DOSSIERS="" AUDIT_DB="" SIGN_KEY="" CLUSTER=1
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=${2:?--out needs a directory}; shift 2 ;;
    --preflight) PREFLIGHT=${2:?--preflight needs a directory}; shift 2 ;;
    --import-records) IMPORTS=${2:?--import-records needs a directory}; shift 2 ;;
    --dossiers) DOSSIERS=${2:?--dossiers needs a directory}; shift 2 ;;
    --audit-db) AUDIT_DB=${2:?--audit-db needs a connection string}; shift 2 ;;
    --sign) SIGN_KEY=${2:?--sign needs a cosign key}; shift 2 ;;
    --no-cluster) CLUSTER=0; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

REPO=$(cd "$(dirname "$0")/../.." && pwd)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NAME=evidence-pack-$STAMP
WORK=$(mktemp -d)
PACK=$WORK/$NAME
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$PACK"/{docs,config,preflight,supply-chain,models,cluster,audit-db} "$OUT"
: > "$PACK/GAPS.txt"

gap() { echo "$*" >> "$PACK/GAPS.txt"; echo "GAP: $*" >&2; }

# Copy files matching a glob from a directory, validating JSON as we go.
collect() {
  local src=$1 pattern=$2 dest=$3 label=$4 n=0 f
  if [ -z "$src" ]; then gap "$label: not supplied"; return; fi
  [ -d "$src" ] || { gap "$label: $src is not a directory"; return; }
  for f in "$src"/$pattern; do
    [ -f "$f" ] || continue
    case "$f" in
      *.json) python3 -m json.tool "$f" >/dev/null 2>&1 || { gap "$label: invalid JSON $f"; continue; } ;;
      *.jsonl) python3 -c 'import json,sys; [json.loads(l) for l in open(sys.argv[1]) if l.strip()]' "$f" \
                 2>/dev/null || { gap "$label: invalid JSON lines $f"; continue; } ;;
    esac
    cp "$f" "$dest/"
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] || gap "$label: no $pattern files in $src"
  echo "$label: $n file(s)"
}

# --- 1. Documents and the configuration they describe -----------------------
cp "$REPO"/docs/evidence-pack/*.md "$PACK/docs/"
cp "$REPO/docs/threat-model.md" "$PACK/docs/"
[ -f "$REPO/VERSIONS.md" ] && cp "$REPO/VERSIONS.md" "$PACK/config/"
cp "$REPO/apps/gateway_admin/tenants.yaml" "$PACK/config/"
cp "$REPO/apps/audit/migrations/"*.sql "$PACK/config/"
cp "$REPO/platform/security/falco-rules.yaml" "$REPO/platform/security/audit-sink.yaml" "$PACK/config/"
mkdir -p "$PACK/config/policies"
cp "$REPO"/platform/policies/*.yaml "$PACK/config/policies/"
mkdir -p "$PACK/config/cilium"
cp "$REPO"/platform/cilium/*.yaml "$PACK/config/cilium/"
echo "documents and configuration: $(find "$PACK/docs" "$PACK/config" -type f | wc -l) file(s)"

# --- 2. Records produced by earlier chapters --------------------------------
collect "$PREFLIGHT" '*.json' "$PACK/preflight" "preflight (Ch 1)"
collect "$IMPORTS" 'import-record*.json' "$PACK/supply-chain" "import records (Ch 3)"
collect "$IMPORTS" 'signing-record*.jsonl' "$PACK/supply-chain" "image signing records (Ch 10)"
collect "$DOSSIERS" '*.md' "$PACK/models" "model dossiers (Ch 5)"

# --- 3. Live cluster state --------------------------------------------------
kube() {
  local file=$1; shift
  if kubectl "$@" > "$PACK/cluster/$file" 2>"$WORK/kubectl.err"; then
    echo "cluster: $file"
  else
    gap "cluster: kubectl $* failed: $(head -c 300 "$WORK/kubectl.err")"
    rm -f "$PACK/cluster/$file"
  fi
}
if [ "$CLUSTER" = 1 ]; then
  if command -v kubectl >/dev/null; then
    kube nodes.txt get nodes -o wide
    kube namespaces.yaml get namespaces -o yaml
    kube clusterpolicies.yaml get clusterpolicies.kyverno.io -o yaml
    kube policyreports.json get policyreports.wgpolicyk8s.io -A -o json
    kube clusterpolicyreports.json get clusterpolicyreports.wgpolicyk8s.io -o json
    kube cilium-network-policies.yaml get ciliumnetworkpolicies.cilium.io -A -o yaml
    kube cilium-clusterwide-policies.yaml get ciliumclusterwidenetworkpolicies.cilium.io -o yaml
    kube enclave-cosign-pub.yaml -n kyverno get configmap enclave-cosign-pub -o yaml
    kube falco-rules-configmap.yaml -n falco get configmap falco-sovereign-rules -o yaml
    # What is actually running, by digest: the answer to "is this the image you signed?"
    kube running-images.tsv get pods -A -o \
      'jsonpath={range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.image}{"@"}{.imageID}{" "}{end}{"\n"}{end}'
  else
    gap "cluster: kubectl not found"
  fi
else
  gap "cluster: skipped (--no-cluster)"
fi

# --- 4. Audit database: retention ran, who can read what ---------------------
if [ -n "$AUDIT_DB" ]; then
  if command -v psql >/dev/null; then
    q() {
      local file=$1 sql=$2
      if psql "$AUDIT_DB" -v ON_ERROR_STOP=1 --csv -c "$sql" > "$PACK/audit-db/$file" 2>"$WORK/psql.err"; then
        echo "audit-db: $file"
      else
        gap "audit-db: $file failed: $(head -c 300 "$WORK/psql.err")"
        rm -f "$PACK/audit-db/$file"
      fi
    }
    q purge-log-90d.csv "SELECT * FROM audit.purge_log WHERE run_at > now() - interval '90 days' ORDER BY run_at"
    q volume-by-tenant-30d.csv "SELECT date_trunc('day', event_time) AS day, tenant, count(*) AS requests,
        count(*) FILTER (WHERE NOT end_user_verified) AS unverified_user,
        count(*) FILTER (WHERE flags @> '{prompt_injection_suspected}') AS injection_flags
      FROM audit.events WHERE event_time > now() - interval '30 days' GROUP BY 1, 2 ORDER BY 1, 2"
    q redactions-30d.csv "SELECT tenant, key, sum(value::int) AS redacted
      FROM audit.events, jsonb_each_text(redactions)
      WHERE event_time > now() - interval '30 days' GROUP BY 1, 2 ORDER BY 1, 2"
    q table-grants.csv "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants
      WHERE table_schema = 'audit' ORDER BY 1, 2, 3"
    q legal-holds.csv "SELECT hold_id, tenant, from_time, to_time, reason, created_by, created_at, released_at
      FROM audit.legal_holds ORDER BY hold_id"
  else
    gap "audit-db: psql not found"
  fi
else
  gap "audit-db: --audit-db not supplied"
fi

# --- 5. Seal ------------------------------------------------------------------
python3 - "$PACK/manifest.json" "$STAMP" "$(id -un)@$(hostname)" "$CLUSTER" <<'PY'
import json, sys
path, stamp, who, cluster = sys.argv[1:5]
gaps = [l.strip() for l in open(path.rsplit("/", 1)[0] + "/GAPS.txt") if l.strip()]
json.dump({"pack": f"evidence-pack-{stamp}", "created_utc": stamp, "collected_by": who,
           "cluster_evidence": cluster == "1", "gaps": gaps}, open(path, "w"), indent=2)
PY
# Byte-order sorted, so two people sealing the same files get the same list.
( cd "$PACK" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$WORK/SHA256SUMS" )
mv "$WORK/SHA256SUMS" "$PACK/SHA256SUMS"
if [ -n "$SIGN_KEY" ]; then
  : "${COSIGN_PASSWORD:?set COSIGN_PASSWORD for --sign}"
  cosign sign-blob --yes --key "$SIGN_KEY" --tlog-upload=false \
    --output-signature "$PACK/SHA256SUMS.sig" "$PACK/SHA256SUMS" >/dev/null 2>&1 \
    || { echo "signing failed" >&2; exit 1; }
  echo "signed: SHA256SUMS.sig"
fi
tar -C "$WORK" -czf "$OUT/$NAME.tar.gz" "$NAME"
( cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )

echo "PACK: $OUT/$NAME.tar.gz ($(wc -l < "$PACK/SHA256SUMS") files, $(grep -c . "$PACK/GAPS.txt" || true) gap(s))"

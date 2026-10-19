#!/usr/bin/env bash
# =============================================================================
# dr_drill.sh — a timed restore of the platform's data into a scratch
# namespace, with measured RTO and RPO
#
# Book:    Sovereign AI Stacks, Chapter 12
# Runs on: the enclave admin workstation (kubectl 1.37, envsubst, python3)
# Needs:   ops/backup/* applied; Velero 1.18.4, CloudNativePG 1.30.1 with the
#          Barman Cloud plugin v0.15.1, backups already on backup.enclave.internal
#
# Usage:   ./dr_drill.sh [--soak SECONDS] [--rto-target 60m] [--rpo-target 15m]
#                        [--rpo-for COMPONENT=DURATION ...] [--keep] [--out DIR]
#          Databases are archived continuously and held to --rpo-target; the
#          nightly sources (chat-ui, qdrant) default to --rpo-for ...=26h.
#
# What it does:
#   1. Writes a heartbeat row every 10 s into the "postgres" database of each
#      production CNPG cluster for --soak seconds (default 600). The row's
#      timestamp is how RPO is measured: it travels in the same WAL stream as
#      the application data.
#   2. Declares the disaster (T0) and records, for each cluster, how far the
#      WAL archive had got at that instant.
#   3. Recovers every database to exactly that point, restores chat-ui (with
#      volume data) from the latest tenant-data backup, and re-hashes the
#      latest Qdrant snapshot run, all into/alongside namespace dr-drill.
#   4. Verifies each restored component with a real query, writes
#      events.jsonl, and renders report.md with drill_report.py.
#   5. Deletes everything it created unless --keep is given.
#
# Exit code: 0 drill passed both targets; 1 failed or missed a target; 2 usage.
# =============================================================================
set -Eeuo pipefail

usage() {
  echo "usage: dr_drill.sh [--soak SECONDS] [--rto-target DURATION] [--rpo-target DURATION]" \
       "[--rpo-for COMPONENT=DURATION ...] [--keep] [--out DIR]" >&2
  exit 2
}

SOAK=600
RTO_TARGET=60m
RPO_TARGET=15m
RPO_FOR=(--rpo-for chat-ui=26h --rpo-for qdrant=26h)
KEEP=0
DRILL_ID=drill-$(date -u +%Y%m%dt%H%Mz)
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --soak) SOAK=${2:?}; shift 2 ;;
    --rto-target) RTO_TARGET=${2:?}; shift 2 ;;
    --rpo-target) RPO_TARGET=${2:?}; shift 2 ;;
    --rpo-for) RPO_FOR+=(--rpo-for "${2:?}"); shift 2 ;;
    --keep) KEEP=1; shift ;;
    --out) OUT=${2:?}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[[ "$SOAK" =~ ^[0-9]+$ ]] || usage

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
TMPL="$REPO/ops/backup/drill"
OUT=${OUT:-$REPO/ops/drills/$DRILL_ID}
EVENTS="$OUT/events.jsonl"
DRILL_NS=dr-drill
# namespace/cluster pairs for every production database
DATABASES=(ai-gateway/litellm-db identity/keycloak-db ai-gateway/audit-db)

for bin in kubectl envsubst python3; do
  command -v "$bin" >/dev/null || { echo "$bin not found" >&2; exit 1; }
done
mkdir -p "$OUT"
: > "$EVENTS"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# event <component> <event> [json-fragment], e.g. event litellm-db check '"ok":true,"detail":"..."'
event() {
  local extra=${3:+,$3}
  printf '{"ts":"%s","component":"%s","event":"%s"%s}\n' "$(now)" "$1" "$2" "$extra" >> "$EVENTS"
  echo "[$(now)] $1 $2 ${3:-}"
}
json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
check() {  # check <component> <ok:true|false> <detail>
  event "$1" check "\"ok\":$2,\"detail\":$(json_str "$3")"
}
fail() { event "$1" failed "\"detail\":$(json_str "$2")"; }
primary() { kubectl -n "$1" get cluster "$2" -o jsonpath='{.status.currentPrimary}'; }
psql_in() {  # psql_in <namespace> <pod> <database> <sql>
  kubectl -n "$1" exec "$2" -c postgres -- psql -X -q -t -A -d "$3" -c "$4"
}

HB_PID=""
# shellcheck disable=SC2329 # invoked by the EXIT trap
cleanup() {
  [ -n "$HB_PID" ] && kill "$HB_PID" 2>/dev/null || true
  if [ "$KEEP" = "1" ]; then
    echo "--keep: leaving namespace $DRILL_NS and drill objects in place"
    return
  fi
  echo "cleaning up drill objects"
  # Ceph volumes use reclaimPolicy Retain: without this, every drill would
  # leave its restored volumes behind as Released PVs and orphaned RBD images.
  kubectl get pv -o jsonpath="{range .items[?(@.spec.claimRef.namespace=='$DRILL_NS')]}{.metadata.name}{'\n'}{end}" \
    | while read -r pv; do
        [ -n "$pv" ] && kubectl patch pv "$pv" -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}' >/dev/null
      done
  kubectl delete namespace "$DRILL_NS" --ignore-not-found --wait=false
  kubectl -n velero delete restore "$DRILL_ID-chat-ui" --ignore-not-found
  kubectl -n rag delete job "$DRILL_ID-qdrant-verify" --ignore-not-found
}
trap cleanup EXIT

# --- 0. preflight -------------------------------------------------------------
phase=$(kubectl -n velero get backupstoragelocation enclave-backup -o jsonpath='{.status.phase}')
[ "$phase" = "Available" ] || { echo "BackupStorageLocation enclave-backup is '$phase', not Available" >&2; exit 1; }
for db in "${DATABASES[@]}"; do
  ns=${db%/*}; c=${db#*/}
  kubectl -n "$ns" get objectstore "$c-store" >/dev/null || { echo "no ObjectStore $c-store in $ns" >&2; exit 1; }
done
BACKUP=$(kubectl -n velero get backups -l velero.io/schedule-name=tenant-data \
  -o jsonpath='{range .items[?(@.status.phase=="Completed")]}{.status.startTimestamp}{" "}{.metadata.name}{"\n"}{end}' \
  | sort | tail -1 | cut -d' ' -f2)
[ -n "$BACKUP" ] || { echo "no Completed tenant-data backup to restore" >&2; exit 1; }

kubectl create namespace "$DRILL_NS" --dry-run=client -o yaml \
  | kubectl label --local -f - pod-security.kubernetes.io/enforce=restricted sovereign-ai/dr-drill="$DRILL_ID" -o yaml \
  | kubectl apply -f -
# Copy the restore credentials and CA into the drill namespace without
# printing them: values go through variables straight into kubectl.
ak=$(kubectl -n ai-gateway get secret cnpg-backup-s3 -o jsonpath='{.data.ACCESS_KEY_ID}' | base64 -d)
sk=$(kubectl -n ai-gateway get secret cnpg-backup-s3 -o jsonpath='{.data.ACCESS_SECRET_KEY}' | base64 -d)
kubectl -n "$DRILL_NS" create secret generic cnpg-backup-s3 \
  --from-literal=ACCESS_KEY_ID="$ak" --from-literal=ACCESS_SECRET_KEY="$sk" --dry-run=client -o yaml | kubectl apply -f -
unset ak sk
kubectl -n ai-gateway get secret backup-appliance-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > "$OUT/ca.crt"
kubectl -n "$DRILL_NS" create secret generic backup-appliance-ca --from-file=ca.crt="$OUT/ca.crt" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f "$OUT/ca.crt"

# --- 1. heartbeats ---------------------------------------------------------------
HB_SQL="CREATE TABLE IF NOT EXISTS dr_drill_heartbeat (drill text NOT NULL, written_at timestamptz NOT NULL DEFAULT now());
INSERT INTO dr_drill_heartbeat (drill) VALUES ('$DRILL_ID');"
heartbeat() {
  while true; do
    for db in "${DATABASES[@]}"; do
      ns=${db%/*}; c=${db#*/}
      psql_in "$ns" "$(primary "$ns" "$c")" postgres "$HB_SQL" >/dev/null 2>&1 || echo "heartbeat to $c failed" >&2
    done
    sleep 10
  done
}
echo "== soaking for ${SOAK}s with heartbeats every 10s"
heartbeat &
HB_PID=$!
sleep "$SOAK"

# --- 2. declare the disaster -------------------------------------------------------
kill "$HB_PID"; wait "$HB_PID" 2>/dev/null || true; HB_PID=""
event drill meta "\"detail\":\"$DRILL_ID\""
event drill declared
T0=$(now)

# The archive frontier: the first LSN NOT yet in the archive at T0. WAL
# segment names are TTTTTTTTXXXXXXXXYYYYYYYY; the next segment starts at
# XXXXXXXX/(YYYYYYYY+1)*16MiB (CNPG's default 16 MiB segment size).
frontier() {
  local wal=$1 hi lo
  [[ "$wal" =~ ^[0-9A-F]{24}$ ]] || return 1
  hi=$((16#${wal:8:8})); lo=$((16#${wal:16:8} + 1))
  if [ "$lo" -ge 256 ]; then hi=$((hi + 1)); lo=0; fi
  printf '%X/%X\n' "$hi" "$((lo * 16777216))"
}
declare -A LSN
for db in "${DATABASES[@]}"; do
  ns=${db%/*}; c=${db#*/}
  wal=$(psql_in "$ns" "$(primary "$ns" "$c")" postgres "SELECT last_archived_wal FROM pg_stat_archiver")
  LSN[$c]=$(frontier "$wal") || { fail "$c" "archiver reports '$wal', not a WAL segment"; LSN[$c]=""; }
done

# --- 3. restore everything at once ------------------------------------------------------
for db in "${DATABASES[@]}"; do
  ns=${db%/*}; c=${db#*/}
  [ -n "${LSN[$c]}" ] || continue
  export CLUSTER=$c DRILL_NS DRILL_ID TARGET_LSN=${LSN[$c]}
  DESTINATION=$(kubectl -n "$ns" get objectstore "$c-store" -o jsonpath='{.spec.configuration.destinationPath}')
  IMAGE=$(kubectl -n "$ns" get cluster "$c" -o jsonpath='{.spec.imageName}')
  SIZE=$(kubectl -n "$ns" get cluster "$c" -o jsonpath='{.spec.storage.size}')
  export DESTINATION IMAGE SIZE
  # shellcheck disable=SC2016 # literal variable list for envsubst
  envsubst '${CLUSTER} ${DRILL_NS} ${DRILL_ID} ${TARGET_LSN} ${DESTINATION} ${IMAGE} ${SIZE}' \
    < "$TMPL/cnpg-restore.yaml.tmpl" | kubectl apply -f -
  event "$c" restore_started
done
export BACKUP DRILL_ID DRILL_NS
# shellcheck disable=SC2016
envsubst '${BACKUP} ${DRILL_ID} ${DRILL_NS}' < "$TMPL/velero-restore.yaml.tmpl" | kubectl apply -f -
event chat-ui restore_started
# shellcheck disable=SC2016
envsubst '${DRILL_ID}' < "$TMPL/qdrant-verify-job.yaml.tmpl" | kubectl apply -f -
event qdrant restore_started

# --- 4. wait and verify ------------------------------------------------------------------
for db in "${DATABASES[@]}"; do
  ns=${db%/*}; c=${db#*/}
  [ -n "${LSN[$c]}" ] || continue
  if ! kubectl -n "$DRILL_NS" wait "cluster/drill-$c" --for=condition=Ready --timeout=60m; then
    fail "$c" "drill-$c did not become Ready within 60m"; continue
  fi
  event "$c" ready
  pod=$(primary "$DRILL_NS" "drill-$c")
  last=$(psql_in "$DRILL_NS" "$pod" postgres \
    "SELECT to_char(max(written_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM dr_drill_heartbeat WHERE drill = '$DRILL_ID'")
  if [ -n "$last" ]; then event "$c" last_data "\"value\":\"$last\""; else fail "$c" "no heartbeat from this drill was recovered"; fi
  appdb=$(psql_in "$DRILL_NS" "$pod" postgres \
    "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1') ORDER BY oid LIMIT 1")
  tables=$(psql_in "$DRILL_NS" "$pod" "$appdb" "SELECT count(*) FROM pg_stat_user_tables")
  if [ "${tables:-0}" -gt 0 ]; then check "$c" true "$appdb has $tables tables"; else check "$c" false "$appdb has no tables"; fi
  case "$c" in
    litellm-db)
      n=$(psql_in "$DRILL_NS" "$pod" "$appdb" 'SELECT count(*) FROM "LiteLLM_VerificationToken"')
      if [ "${n:-0}" -gt 0 ]; then check "$c" true "$n virtual keys"; else check "$c" false "no virtual keys"; fi ;;
    keycloak-db)
      n=$(psql_in "$DRILL_NS" "$pod" "$appdb" "SELECT count(*) FROM realm")
      if [ "${n:-0}" -ge 2 ]; then check "$c" true "$n realms"; else check "$c" false "realm count $n, expected >= 2"; fi ;;
  esac
  event "$c" verified
done

deadline=$(( $(date +%s) + 3600 ))
until phase=$(kubectl -n velero get restore "$DRILL_ID-chat-ui" -o jsonpath='{.status.phase}'); \
      [[ "$phase" =~ ^(Completed|PartiallyFailed|Failed|FailedValidation)$ ]]; do
  [ "$(date +%s)" -lt "$deadline" ] || { phase=Timeout; break; }
  sleep 15
done
if [ "$phase" = "Completed" ] && \
   kubectl -n "$DRILL_NS" wait pod -l app.kubernetes.io/name=open-webui --for=condition=Ready --timeout=20m; then
  event chat-ui ready
  started=$(kubectl -n velero get backup "$BACKUP" -o jsonpath='{.status.startTimestamp}')
  event chat-ui last_data "\"value\":\"$started\""
  for pod in $(kubectl -n "$DRILL_NS" get pods -l app.kubernetes.io/name=open-webui -o name); do
    n=$(kubectl -n "$DRILL_NS" exec "$pod" -c open-webui -- python3 -c \
      "import sqlite3; print(sqlite3.connect('file:/app/backend/data/velero/webui.db?mode=ro', uri=True).execute('select count(*) from chat').fetchone()[0])" \
      2>/dev/null || echo "")
    if [ -n "$n" ]; then check chat-ui true "${pod#pod/}: $n chats"; else check chat-ui false "${pod#pod/}: webui.db copy unreadable"; fi
  done
  event chat-ui verified
else
  fail chat-ui "restore phase $phase"
fi

if kubectl -n rag wait "job/$DRILL_ID-qdrant-verify" --for=condition=complete --timeout=120m; then
  event qdrant ready
  result=$(kubectl -n rag logs "job/$DRILL_ID-qdrant-verify" | tail -1)
  created=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["created_at"])' "$result")
  verified=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["verified"])' "$result")
  event qdrant last_data "\"value\":\"$created\""
  check qdrant true "$verified snapshot objects re-hashed and matched the manifest"
  event qdrant verified
else
  fail qdrant "snapshot verification failed: $(kubectl -n rag logs "job/$DRILL_ID-qdrant-verify" --tail=3 2>&1 | tr '\n' ' ')"
fi

# --- 5. report -----------------------------------------------------------------------------
for db in "${DATABASES[@]}"; do
  ns=${db%/*}; c=${db#*/}
  psql_in "$ns" "$(primary "$ns" "$c")" postgres "DELETE FROM dr_drill_heartbeat WHERE drill = '$DRILL_ID'" >/dev/null || true
done
echo "== declared at $T0; report in $OUT/report.md"
set +e
python3 "$HERE/drill_report.py" "$EVENTS" --rto-target "$RTO_TARGET" --rpo-target "$RPO_TARGET" "${RPO_FOR[@]}" \
  --markdown "$OUT/report.md" --json "$OUT/report.json"
rc=$?
set -e
exit "$rc"

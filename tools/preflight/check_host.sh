#!/usr/bin/env bash
# =============================================================================
# check_host.sh — Preflight checks for a Sovereign AI Stacks node
#
# Book:    Sovereign AI Stacks, Chapter 1
# Purpose: Verify that a machine meets the hardware, OS and network-isolation
#          requirements for its role BEFORE anything is installed on it.
#
# Usage:
#   ./check_host.sh --profile <lab|budget|control-plane|gpu-worker|transfer>
#                   [--egress <expect-open|expect-closed|ignore>]
#                   [--data-path <dir>] [--json]
#
# Exit codes:
#   0  all checks passed (warnings allowed)
#   1  one or more checks FAILED
#   2  usage error
#
# Tested with: bash 5.1+, GNU coreutils 9.x, NVIDIA driver 595.x (nvidia-smi)
# =============================================================================
set -Eeuo pipefail

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
PROFILE=""
EGRESS_MODE=""            # resolved from profile when not given explicitly
DATA_PATH="/var"          # where container images and model weights will live
OUTPUT_JSON=0
MIN_KERNEL="6.1"          # oldest kernel line we support for Cilium eBPF features

# Endpoints a sovereign node must NOT be able to reach. If any of these answer
# from inside the enclave, the air gap is broken. host:port pairs.
EGRESS_PROBES=(
  "huggingface.co:443"
  "registry-1.docker.io:443"
  "ghcr.io:443"
  "nvcr.io:443"
  "pypi.org:443"
  "api.openai.com:443"
  "1.1.1.1:53"
  "8.8.8.8:53"
)

# Result accumulators (parallel arrays keep this portable to bash 4+)
declare -a R_NAME=() R_STATUS=() R_DETAIL=()
FAILURES=0
WARNINGS=0

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
usage() {
  sed -n '9,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

# record <name> <PASS|WARN|FAIL|INFO> <detail>
record() {
  R_NAME+=("$1")
  R_STATUS+=("$2")
  R_DETAIL+=("$3")
  case "$2" in
    FAIL) FAILURES=$((FAILURES + 1)) ;;
    WARN) WARNINGS=$((WARNINGS + 1)) ;;
  esac
}

# version_ge <a> <b> : true when version a >= version b
version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# json_escape <string> : minimal escaping for the values this script emits
json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\t'/ }
  s=${s//$'\n'/ }
  printf '%s' "$s"
}

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)   PROFILE=${2:-}; shift 2 ;;
    --egress)    EGRESS_MODE=${2:-}; shift 2 ;;
    --data-path) DATA_PATH=${2:-}; shift 2 ;;
    --json)      OUTPUT_JSON=1; shift ;;
    -h|--help)   usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$PROFILE" ] || { echo "--profile is required" >&2; usage; }

# ----------------------------------------------------------------------------
# Profile thresholds
#   MIN_CPU        logical cores
#   MIN_RAM_GIB    installed memory
#   MIN_DISK_GIB   free space on DATA_PATH
#   GPU_REQUIRED   1 = node must have NVIDIA GPUs
#   MIN_GPU_COUNT  number of GPUs
#   MIN_GPU_MIB    VRAM on the smallest GPU (MiB, as nvidia-smi reports it)
#   MIN_VRAM_MIB   total VRAM across all GPUs (MiB)
# ----------------------------------------------------------------------------
case "$PROFILE" in
  lab)
    MIN_CPU=16;  MIN_RAM_GIB=120;  MIN_DISK_GIB=1500
    GPU_REQUIRED=1; MIN_GPU_COUNT=1; MIN_GPU_MIB=22000; MIN_VRAM_MIB=45000
    DEFAULT_EGRESS="ignore" ;;
  budget)
    MIN_CPU=8;   MIN_RAM_GIB=60;   MIN_DISK_GIB=1000
    GPU_REQUIRED=1; MIN_GPU_COUNT=1; MIN_GPU_MIB=22000; MIN_VRAM_MIB=22000
    DEFAULT_EGRESS="ignore" ;;
  control-plane)
    MIN_CPU=8;   MIN_RAM_GIB=30;   MIN_DISK_GIB=200
    GPU_REQUIRED=0; MIN_GPU_COUNT=0; MIN_GPU_MIB=0; MIN_VRAM_MIB=0
    DEFAULT_EGRESS="expect-closed" ;;
  gpu-worker)
    MIN_CPU=64;  MIN_RAM_GIB=1000; MIN_DISK_GIB=3000
    GPU_REQUIRED=1; MIN_GPU_COUNT=8; MIN_GPU_MIB=79000; MIN_VRAM_MIB=632000
    DEFAULT_EGRESS="expect-closed" ;;
  transfer)
    MIN_CPU=4;   MIN_RAM_GIB=15;   MIN_DISK_GIB=2000
    GPU_REQUIRED=0; MIN_GPU_COUNT=0; MIN_GPU_MIB=0; MIN_VRAM_MIB=0
    DEFAULT_EGRESS="expect-open" ;;
  *)
    echo "Unknown profile: $PROFILE" >&2; usage ;;
esac

EGRESS_MODE=${EGRESS_MODE:-$DEFAULT_EGRESS}
case "$EGRESS_MODE" in
  expect-open|expect-closed|ignore) ;;
  *) echo "Invalid --egress value: $EGRESS_MODE" >&2; usage ;;
esac

# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------
check_os() {
  local os arch kernel
  os=$(uname -s)
  arch=$(uname -m)
  kernel=$(uname -r | cut -d- -f1)

  if [ "$os" != "Linux" ]; then
    record "os" FAIL "Linux required, found $os"
    return
  fi
  case "$arch" in
    x86_64|aarch64) record "arch" PASS "$arch" ;;
    *)              record "arch" FAIL "unsupported architecture $arch" ;;
  esac
  if version_ge "$kernel" "$MIN_KERNEL"; then
    record "kernel" PASS "$kernel (>= $MIN_KERNEL)"
  else
    record "kernel" FAIL "$kernel is older than $MIN_KERNEL"
  fi
}

check_cpu() {
  local cores
  cores=$(nproc --all)
  if [ "$cores" -ge "$MIN_CPU" ]; then
    record "cpu" PASS "$cores logical cores (>= $MIN_CPU)"
  else
    record "cpu" FAIL "$cores logical cores, need $MIN_CPU"
  fi
}

check_ram() {
  local kib gib
  kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  gib=$((kib / 1024 / 1024))
  if [ "$gib" -ge "$MIN_RAM_GIB" ]; then
    record "memory" PASS "${gib} GiB (>= $MIN_RAM_GIB)"
  else
    record "memory" FAIL "${gib} GiB, need $MIN_RAM_GIB"
  fi
}

check_swap() {
  local swap_kib
  swap_kib=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
  if [ "$swap_kib" -eq 0 ]; then
    record "swap" PASS "disabled"
  else
    record "swap" WARN "$((swap_kib / 1024)) MiB swap enabled; kubelet expects swap off unless NodeSwap is configured"
  fi
}

check_disk() {
  local free_kib free_gib
  if [ ! -d "$DATA_PATH" ]; then
    record "disk" FAIL "data path $DATA_PATH does not exist"
    return
  fi
  free_kib=$(df -Pk "$DATA_PATH" | awk 'NR==2 {print $4}')
  free_gib=$((free_kib / 1024 / 1024))
  if [ "$free_gib" -ge "$MIN_DISK_GIB" ]; then
    record "disk" PASS "${free_gib} GiB free on $DATA_PATH (>= $MIN_DISK_GIB)"
  else
    record "disk" FAIL "${free_gib} GiB free on $DATA_PATH, need $MIN_DISK_GIB"
  fi
}

check_gpu() {
  if [ "$GPU_REQUIRED" -eq 0 ]; then
    record "gpu" INFO "not required for profile $PROFILE"
    return
  fi
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    record "gpu" FAIL "nvidia-smi not found; install the NVIDIA driver or check PATH"
    return
  fi

  local raw count=0 total=0 smallest=0 names=""
  if ! raw=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>&1); then
    record "gpu" FAIL "nvidia-smi error: $raw"
    return
  fi

  # Each line: "<name>, <MiB>"
  while IFS=',' read -r name mib; do
    mib=${mib// /}
    [ -n "$mib" ] || continue
    count=$((count + 1))
    total=$((total + mib))
    if [ "$smallest" -eq 0 ] || [ "$mib" -lt "$smallest" ]; then
      smallest=$mib
    fi
    names="${names:+$names; }${name# } (${mib} MiB)"
  done <<< "$raw"

  record "gpu-inventory" INFO "$count GPU(s): $names"

  if [ "$count" -ge "$MIN_GPU_COUNT" ]; then
    record "gpu-count" PASS "$count (>= $MIN_GPU_COUNT)"
  else
    record "gpu-count" FAIL "$count, need $MIN_GPU_COUNT"
  fi
  if [ "$smallest" -ge "$MIN_GPU_MIB" ]; then
    record "gpu-vram-each" PASS "smallest GPU ${smallest} MiB (>= $MIN_GPU_MIB)"
  else
    record "gpu-vram-each" FAIL "smallest GPU ${smallest} MiB, need $MIN_GPU_MIB"
  fi
  if [ "$total" -ge "$MIN_VRAM_MIB" ]; then
    record "gpu-vram-total" PASS "${total} MiB total (>= $MIN_VRAM_MIB)"
  else
    record "gpu-vram-total" FAIL "${total} MiB total, need $MIN_VRAM_MIB"
  fi
}

check_time() {
  # Clock skew breaks TLS, OIDC token validation and cosign verification.
  if command -v timedatectl >/dev/null 2>&1; then
    local synced
    synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo "unknown")
    case "$synced" in
      yes) record "time-sync" PASS "NTP synchronised" ;;
      no)  record "time-sync" WARN "clock not synchronised; enclave needs an internal NTP source" ;;
      *)   record "time-sync" WARN "could not determine NTP state" ;;
    esac
  else
    record "time-sync" WARN "timedatectl unavailable; verify time sync manually"
  fi
}

# probe_tcp <host> <port> : true when a TCP connection succeeds within 3s
probe_tcp() {
  timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" >/dev/null 2>&1
}

check_egress() {
  if [ "$EGRESS_MODE" = "ignore" ]; then
    record "egress" INFO "egress probes skipped (mode: ignore)"
    return
  fi

  local target host port reachable=() unreachable=()
  for target in "${EGRESS_PROBES[@]}"; do
    host=${target%:*}
    port=${target##*:}
    if probe_tcp "$host" "$port"; then
      reachable+=("$target")
    else
      unreachable+=("$target")
    fi
  done

  if [ "$EGRESS_MODE" = "expect-closed" ]; then
    if [ "${#reachable[@]}" -eq 0 ]; then
      record "egress" PASS "all ${#EGRESS_PROBES[@]} external probes blocked"
    else
      record "egress" FAIL "air gap breached; reachable: ${reachable[*]}"
    fi
  else
    if [ "${#unreachable[@]}" -eq 0 ]; then
      record "egress" PASS "all ${#EGRESS_PROBES[@]} upstream endpoints reachable"
    else
      record "egress" WARN "transfer station cannot reach: ${unreachable[*]}"
    fi
  fi
}

# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
check_os
check_cpu
check_ram
check_swap
check_disk
check_gpu
check_time
check_egress

# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------
if [ "$OUTPUT_JSON" -eq 1 ]; then
  printf '{"host":"%s","profile":"%s","egress_mode":"%s","failures":%d,"warnings":%d,"checks":[' \
    "$(json_escape "$(hostname)")" "$PROFILE" "$EGRESS_MODE" "$FAILURES" "$WARNINGS"
  for i in "${!R_NAME[@]}"; do
    [ "$i" -gt 0 ] && printf ','
    printf '{"name":"%s","status":"%s","detail":"%s"}' \
      "$(json_escape "${R_NAME[$i]}")" "${R_STATUS[$i]}" "$(json_escape "${R_DETAIL[$i]}")"
  done
  printf ']}\n'
else
  printf 'Sovereign AI Stacks preflight  host=%s  profile=%s  egress=%s\n' \
    "$(hostname)" "$PROFILE" "$EGRESS_MODE"
  printf '%-16s %-5s %s\n' "CHECK" "STATE" "DETAIL"
  for i in "${!R_NAME[@]}"; do
    printf '%-16s %-5s %s\n' "${R_NAME[$i]}" "${R_STATUS[$i]}" "${R_DETAIL[$i]}"
  done
  printf 'Result: %d failure(s), %d warning(s)\n' "$FAILURES" "$WARNINGS"
fi

[ "$FAILURES" -eq 0 ]

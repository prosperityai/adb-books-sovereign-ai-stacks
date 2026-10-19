#!/usr/bin/env bash
# =============================================================================
# upgrade_model.sh — blue/green model upgrades: stage by digest, canary through
# the gateway, promote or roll back with one command
#
# Book:    Sovereign AI Stacks, Chapter 12
# Runs on: the enclave admin workstation (kubectl 1.37, helm 3.22, oras 1.3.x,
#          python3 with PyYAML 6.0)
#
# Usage:
#   upgrade_model.sh stage    <model> <harbor-ref@sha256:digest>   # green Deployment, exposed as <alias>-candidate
#   upgrade_model.sh canary   <model> <percent>                    # needs a passed eval; weight the alias
#   upgrade_model.sh analyse  <model> [window]                     # Prometheus verdict (default 15m)
#   upgrade_model.sh promote  <model>                              # green becomes stable
#   upgrade_model.sh rollback <model>                              # undo the canary, or the last promotion
#   upgrade_model.sh status   <model>
#
#   <model> is the Deployment name without "vllm-", e.g. qwen3-8-27b.
#
# Promotion gate: Chapter 11's acceptance run against qwen3.8-27b-candidate
# must pass. `canary` and `promote` refuse to run unless the Job
# evals/eval-<model>-candidate succeeded AFTER the canary was staged.
#
# Environment:
#   PROM_URL   Prometheus base URL for `analyse` [http://127.0.0.1:9090, via port-forward]
#   GATEWAY_CHART [oci://harbor.enclave.internal/charts/litellm-helm]  GATEWAY_VERSION [1.106.0]
#
# The model weights reach Harbor through the Chapter 3 supply chain, like
# everything else: verify_bundle.sh, then push_model_oci.sh, which prints the
# digest this script needs. Nothing here ever pulls from outside the enclave.
# =============================================================================
set -Eeuo pipefail

usage() { sed -n '/^# Usage:/,/^#   <model>/p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 2 ] || usage
CMD=$1 MODEL=$2
shift 2

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
NS=inference
DEPLOY=vllm-$MODEL
CANARY=$DEPLOY-canary
STABLE_BASE=http://$DEPLOY.$NS.svc:8000/v1
CANARY_BASE=http://$CANARY.$NS.svc:8000/v1
EVAL_JOB=eval-$MODEL-candidate
VALUES=$REPO/platform/gateway/litellm-values.yaml
GATEWAY_CHART=${GATEWAY_CHART:-oci://harbor.enclave.internal/charts/litellm-helm}
GATEWAY_VERSION=${GATEWAY_VERSION:-1.106.0}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

for bin in kubectl helm python3; do
  command -v "$bin" >/dev/null || { echo "$bin not found" >&2; exit 1; }
done
kubectl -n "$NS" get deploy "$DEPLOY" >/dev/null || { echo "no Deployment $DEPLOY in $NS" >&2; exit 1; }
ALIAS=$(kubectl -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.spec.template.metadata.labels.sovereign-ai/model}')
[ -n "$ALIAS" ] || { echo "$DEPLOY has no sovereign-ai/model label" >&2; exit 1; }

log() { echo "== $*"; }
canary_exists() { kubectl -n "$NS" get deploy "$CANARY" >/dev/null 2>&1; }
model_ref() {  # model_ref <deployment>
  kubectl -n "$NS" get deploy "$1" \
    -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="model-fetch")].env[?(@.name=="MODEL_REF")].value}'
}

# gateway off  = the configuration in Git, no canary anywhere
# gateway 0    = canary reachable only as <alias>-candidate (evaluation)
# gateway 1-99 = plus that share of the production alias
# gateway 100  = everything on the stable backend moved to the canary
gateway() {
  local weight=$1 files=(-f "$VALUES")
  if [ "$weight" != "off" ]; then
    python3 "$HERE/canary.py" overlay --values "$VALUES" --alias "$ALIAS" \
      --stable-base "$STABLE_BASE" --canary-base "$CANARY_BASE" --weight "$weight" > "$WORK/overlay.yaml"
    files+=(-f "$WORK/overlay.yaml")
    log "gateway: $ALIAS-candidate -> canary; $ALIAS -> canary ${weight}%"
  else
    log "gateway: back to the reviewed values (no canary)"
  fi
  # Only the reviewed values file (plus the generated overlay) is applied, so
  # "off" is exactly the configuration in Git. The chart's config checksum
  # rolls the gateway pods one at a time.
  helm upgrade litellm "$GATEWAY_CHART" --version "$GATEWAY_VERSION" -n ai-gateway "${files[@]}" \
    --wait --timeout 10m >/dev/null
}

smoke() {  # smoke <base-url>
  log "smoke test $1 (sw, en, ar) from inside the gateway pod"
  # Runs where the network policy allows model-server traffic, with the key
  # the gateway already holds. canary.py's smoke path is stdlib-only.
  kubectl -n ai-gateway exec -i deploy/litellm -- python3 - smoke --base-url "$1" --model "$ALIAS" \
    < "$HERE/canary.py"
}

need_gpus() {
  local need free
  need=$(kubectl -n "$NS" get deploy "$DEPLOY" -o json \
    | python3 -c 'import json,sys; sys.path.insert(0, sys.argv[1]); import canary; print(canary.gpus_requested(json.load(sys.stdin)))' "$HERE")
  free=$(kubectl get nodes,pods -A -o json | python3 "$HERE/canary.py" gpu-free | sort -k2 -n | tail -1 | cut -d' ' -f2)
  echo "$need ${free:-0}"
}

stage() {  # stage <ref@digest>
  local ref=$1 claim size need free
  canary_exists && { echo "$CANARY already exists; promote or roll it back first" >&2; exit 1; }
  read -r need free < <(need_gpus)
  [ "$free" -ge "$need" ] || { echo "canary needs $need GPU(s) on one node; the freest node has $free" >&2; exit 1; }
  if command -v oras >/dev/null; then
    oras manifest fetch --descriptor "$ref" >/dev/null || { echo "$ref is not in Harbor" >&2; exit 1; }
  else
    echo "WARNING: oras not found; not checking that $ref exists in Harbor" >&2
  fi
  claim=$(kubectl -n "$NS" get deploy "$DEPLOY" \
    -o jsonpath='{.spec.template.spec.volumes[?(@.name=="models")].persistentVolumeClaim.claimName}')
  size=$(kubectl -n "$NS" get pvc "$claim" -o jsonpath='{.spec.resources.requests.storage}')
  log "staging $CANARY from $ref ($need GPU, $size cache)"
  kubectl -n "$NS" get deploy "$DEPLOY" -o json \
    | python3 "$HERE/canary.py" render --model-ref "$ref" --cache-size "$size" \
    | kubectl -n "$NS" apply -f -
  kubectl -n "$NS" rollout status "deploy/$CANARY" --timeout=60m
  smoke "$CANARY_BASE"
  gateway 0
  log "staged as $ALIAS-candidate. Next: run Chapter 11's acceptance Job against it:"
  echo "  kubectl -n evals delete job $EVAL_JOB --ignore-not-found && kubectl apply -f platform/evals/eval-job.yaml"
  echo "  kubectl -n evals wait job/$EVAL_JOB --for=condition=complete --timeout=60m"
}

# Chapter 11's acceptance run against <alias>-candidate must pass, and it must
# have run against THIS canary: a pass from last month's candidate is not one.
eval_passed() {
  local staged finished ok
  [ "${SKIP_EVAL:-0}" = "1" ] && { log "eval gate skipped: restoring a model that already passed it"; return 0; }
  staged=$(kubectl -n "$NS" get deploy "$CANARY" -o jsonpath='{.metadata.creationTimestamp}')
  ok=$(kubectl -n evals get job "$EVAL_JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)
  finished=$(kubectl -n evals get job "$EVAL_JOB" -o jsonpath='{.status.completionTime}' 2>/dev/null || true)
  if [ "${ok:-0}" != "1" ] || [ -z "$finished" ] || [ "$(date -d "$finished" +%s)" -lt "$(date -d "$staged" +%s)" ]; then
    echo "BLOCKED: Chapter 11's acceptance run against $ALIAS-candidate has not passed since $CANARY was staged" >&2
    echo "  (job evals/$EVAL_JOB: succeeded=${ok:-none} completed=${finished:-never})" >&2
    exit 1
  fi
  log "eval gate passed: evals/$EVAL_JOB completed $finished"
}

delete_canary() {
  local pv
  pv=$(kubectl -n "$NS" get pvc "model-cache-$MODEL-canary" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)
  kubectl -n "$NS" delete deploy,svc -l sovereign-ai/track=canary,app.kubernetes.io/name="$CANARY" --ignore-not-found
  # The cache volume is Retain (Chapter 4): release it explicitly, or every
  # upgrade leaves 40-90 GB of orphaned weights in Ceph.
  [ -n "$pv" ] && kubectl patch pv "$pv" -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}' >/dev/null
  kubectl -n "$NS" delete pvc "model-cache-$MODEL-canary" --ignore-not-found
}

promote() {
  local new prev
  canary_exists || { echo "no canary to promote" >&2; exit 1; }
  kubectl -n "$NS" rollout status "deploy/$CANARY" --timeout=1m
  eval_passed
  new=$(model_ref "$CANARY")
  prev=$(model_ref "$DEPLOY")
  gateway 100
  log "recreating $DEPLOY on $new while the canary carries all traffic"
  kubectl -n "$NS" set env "deploy/$DEPLOY" -c model-fetch "MODEL_REF=$new"
  kubectl -n "$NS" annotate --overwrite "deploy/$DEPLOY" \
    "sovereign-ai/model-ref=$new" "sovereign-ai/previous-model-ref=$prev"
  kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=60m
  smoke "$STABLE_BASE"
  gateway off
  delete_canary
  log "promoted $new"
  echo "NOW commit the change: set MODEL_REF in platform/inference/$DEPLOY.yaml to"
  echo "  $new"
  echo "Re-applying the old manifest would silently roll the model back."
}

rollback() {
  local prev need free
  if canary_exists; then
    gateway off
    delete_canary
    log "canary removed; $DEPLOY still serves $(model_ref "$DEPLOY")"
    return
  fi
  prev=$(kubectl -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.metadata.annotations.sovereign-ai/previous-model-ref}')
  [ -n "$prev" ] || { echo "no canary and no previous-model-ref annotation: nothing to roll back" >&2; exit 1; }
  read -r need free < <(need_gpus)
  if [ "$free" -ge "$need" ]; then
    log "rolling back to $prev the blue/green way: stage it, then promote"
    stage "$prev"
    SKIP_EVAL=1 promote
  else
    log "no spare GPUs: rolling back in place; ${ALIAS} falls back to gpt-oss-120b while $DEPLOY restarts"
    kubectl -n "$NS" set env "deploy/$DEPLOY" -c model-fetch "MODEL_REF=$prev"
    kubectl -n "$NS" annotate --overwrite "deploy/$DEPLOY" \
      "sovereign-ai/model-ref=$prev" "sovereign-ai/previous-model-ref-"
    kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=60m
    smoke "$STABLE_BASE"
  fi
}

case "$CMD" in
  stage)
    [ $# -eq 1 ] || usage
    stage "$1" ;;
  canary)
    [ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 50 ] \
      || { echo "canary weight must be 1-50 (promote moves it to 100)" >&2; exit 2; }
    canary_exists || { echo "stage a canary first" >&2; exit 1; }
    eval_passed
    smoke "$CANARY_BASE"
    gateway "$1" ;;
  analyse)
    [ $# -le 1 ] || usage
    canary_exists || { echo "no canary running" >&2; exit 1; }
    python3 "$HERE/canary.py" gate --window "${1:-15m}" \
      --stable-selector "namespace=\"$NS\",pod=~\"$DEPLOY-.*\",pod!~\"$CANARY-.*\"" \
      --canary-selector "namespace=\"$NS\",pod=~\"$CANARY-.*\"" ;;
  promote)
    [ $# -eq 0 ] || usage
    promote ;;
  rollback)
    [ $# -eq 0 ] || usage
    rollback ;;
  status)
    echo "stable   $DEPLOY  $(model_ref "$DEPLOY")"
    echo "previous $(kubectl -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.metadata.annotations.sovereign-ai/previous-model-ref}')"
    if canary_exists; then
      echo "canary   $CANARY  $(model_ref "$CANARY")  ready=$(kubectl -n "$NS" get deploy "$CANARY" -o jsonpath='{.status.readyReplicas}')"
    else
      echo "canary   none"
    fi ;;
  *) usage ;;
esac

#!/usr/bin/env bash
# =============================================================================
# run_bench.sh — concurrency sweep against a running vLLM Deployment
#
# Book:    Sovereign AI Stacks, Chapter 5
# Needs:   kubectl with access to the inference namespace, python3
#
# Usage:
#   ./run_bench.sh <deployment> <served-model-name> <model-dir> [concurrency...]
#   ./run_bench.sh vllm-qwen3-8-27b qwen3.8-27b qwen3.8-27b-fp8 1 8 16 32 64
#
# Environment (defaults in brackets):
#   INPUT_LEN [2048]  OUTPUT_LEN [512]  PROMPTS_PER_LEVEL [200]
#   SLO_TTFT_P95_MS [3000]  SLO_TPOT_P95_MS [100]  RESULTS [./results/<deployment>-<UTC timestamp>]
#
# The benchmark client runs INSIDE the vLLM pod (kubectl exec), against
# localhost: no extra image to transfer, no network-policy exception, and the
# tokenizer is already on disk. The client uses one or two CPU cores; the
# server's work is on the GPU, so the interference is small and constant.
# =============================================================================
set -Eeuo pipefail

DEPLOY=${1:?usage: run_bench.sh <deployment> <served-model-name> <model-dir> [concurrency...]}
MODEL=${2:?served model name required}
MODEL_DIR=${3:?model directory name under /models required}
shift 3
LEVELS=("$@")
[ ${#LEVELS[@]} -gt 0 ] || LEVELS=(1 8 16 32 64)

NS=inference
INPUT_LEN=${INPUT_LEN:-2048}
OUTPUT_LEN=${OUTPUT_LEN:-512}
PROMPTS=${PROMPTS_PER_LEVEL:-200}
RESULTS=${RESULTS:-./results/${DEPLOY}-$(date -u +%Y%m%dT%H%M%SZ)}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$RESULTS"

kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=30m

for c in "${LEVELS[@]}"; do
  echo "== concurrency $c: $PROMPTS prompts, ${INPUT_LEN} in / ${OUTPUT_LEN} out"
  # Single quotes: $VLLM_API_KEY is expanded inside the pod, never on this host.
  # shellcheck disable=SC2016
  kubectl -n "$NS" exec "deploy/$DEPLOY" -c vllm -- sh -c '
    OPENAI_API_KEY="$VLLM_API_KEY" vllm bench serve \
      --backend openai-chat --base-url http://127.0.0.1:8000 --endpoint /v1/chat/completions \
      --model "$0" --tokenizer "/models/$1" \
      --dataset-name random --random-input-len "$2" --random-output-len "$3" \
      --num-prompts "$4" --max-concurrency "$5" \
      --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,95,99 \
      --save-result --result-dir /tmp --result-filename bench.json >/dev/null &&
    cat /tmp/bench.json' \
    "$MODEL" "$MODEL_DIR" "$INPUT_LEN" "$OUTPUT_LEN" "$PROMPTS" "$c" > "$RESULTS/c${c}.json"
done

python3 "$HERE/report.py" "$RESULTS"/c*.json \
  --slo-ttft-p95-ms "${SLO_TTFT_P95_MS:-3000}" --slo-tpot-p95-ms "${SLO_TPOT_P95_MS:-100}" \
  | tee "$RESULTS/report.md"

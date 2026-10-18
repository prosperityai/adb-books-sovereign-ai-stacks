#!/usr/bin/env python3
r"""gpu_sizer.py — size GPU servers for LLM inference from first principles.

Book: Sovereign AI Stacks, Chapter 2
Requires: Python 3.13, PyYAML 6.0

The calculator answers three procurement questions:

1. How much VRAM does one active conversation cost (KV cache + recurrent state)?
2. How many GPUs of a given type are needed to hold the weights and serve N
   concurrent sequences at a given context length?
3. What is the memory-bandwidth ceiling on aggregate decode throughput?

Examples:
    python gpu_sizer.py --model qwen3.8-27b --precision fp8 --kv-dtype fp8 \
        --gpu l40s --context 8192 --concurrency 32

    python gpu_sizer.py --model gemma-4-31b --precision bf16 --gpu l40s \
        --context 8192 --users 400 --requests-per-hour 6 --avg-request-seconds 25
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

GIB = 1024**3
GB = 1000**3

# Bytes per element for KV cache and recurrent state.
DTYPE_BYTES: dict[str, float] = {"bf16": 2.0, "fp16": 2.0, "fp8": 1.0}

# Bytes per parameter used ONLY when a precision has no measured checkpoint
# size. The small surcharge on fp8/int4 covers scales and unquantized layers.
PARAM_BYTES_ESTIMATE: dict[str, float] = {
    "bf16": 2.0,
    "fp16": 2.0,
    "fp8": 1.05,
    "int4": 0.56,
}

# Tensor-parallel degrees vLLM supports cleanly on a single node.
TP_DEGREES = (1, 2, 4, 8)

DEFAULT_CATALOGUE = Path(__file__).with_name("models.yaml")


@dataclass(frozen=True)
class AttentionGroup:
    """A set of layers that share one attention shape."""

    layers: int
    kv_heads: int
    head_dim: int
    window: int | None  # None = full attention; int = sliding window in tokens


@dataclass(frozen=True)
class ModelSpec:
    name: str
    params_billion: float
    active_params_billion: float
    max_context: int
    weights_gb: dict[str, float]
    attention: tuple[AttentionGroup, ...]
    linear_layers: int = 0
    linear_elements_per_layer: int = 0


@dataclass(frozen=True)
class GpuSpec:
    name: str
    memory_gib: float
    bandwidth_gbs: float


@dataclass(frozen=True)
class Plan:
    """The sizing result for one tensor-parallel degree."""

    tp: int
    weights_gib_total: float
    weights_estimated: bool
    per_gpu_budget_gib: float
    per_gpu_weights_gib: float
    per_gpu_overhead_gib: float
    per_gpu_cache_gib: float
    seq_cache_gib: float
    max_sequences: int
    fits: bool
    decode_ceiling_tps: float


# --------------------------------------------------------------------------
# Catalogue loading
# --------------------------------------------------------------------------
def load_catalogue(path: Path) -> tuple[dict[str, ModelSpec], dict[str, GpuSpec]]:
    """Parse models.yaml into typed specs. Fails loudly on missing fields."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    models: dict[str, ModelSpec] = {}
    for name, m in raw["models"].items():
        linear = m.get("linear_state") or {}
        models[name] = ModelSpec(
            name=name,
            params_billion=float(m["params_billion"]),
            active_params_billion=float(m["active_params_billion"]),
            max_context=int(m["max_context"]),
            weights_gb={k: float(v) for k, v in m["weights_gb"].items()},
            attention=tuple(
                AttentionGroup(
                    layers=int(g["layers"]),
                    kv_heads=int(g["kv_heads"]),
                    head_dim=int(g["head_dim"]),
                    window=None if g.get("window") is None else int(g["window"]),
                )
                for g in m["attention"]
            ),
            linear_layers=int(linear.get("layers", 0)),
            linear_elements_per_layer=int(linear.get("elements_per_layer", 0)),
        )
    gpus = {name: GpuSpec(name, float(g["memory_gib"]), float(g["bandwidth_gbs"])) for name, g in raw["gpus"].items()}
    return models, gpus


# --------------------------------------------------------------------------
# Core maths
# --------------------------------------------------------------------------
def weights_gib(model: ModelSpec, precision: str) -> tuple[float, bool]:
    """Return (weights in GiB, estimated?) for a precision."""
    if precision in model.weights_gb:
        return model.weights_gb[precision] * GB / GIB, False
    if precision not in PARAM_BYTES_ESTIMATE:
        raise ValueError(f"unknown precision {precision!r}")
    est_bytes = model.params_billion * 1e9 * PARAM_BYTES_ESTIMATE[precision]
    return est_bytes / GIB, True


def kv_bytes_per_sequence(model: ModelSpec, context: int, kv_dtype: str, tp: int = 1) -> float:
    """KV cache bytes for ONE sequence holding `context` tokens, summed over all
    `tp` tensor-parallel ranks.

    Each attention layer stores a key and a value vector per KV head per token:
        2 * kv_heads * head_dim * bytes_per_element
    Sliding-window layers never hold more than `window` tokens.

    When tp exceeds a layer's KV-head count, vLLM replicates KV heads so every
    rank holds at least one; the cluster then stores tp heads, not kv_heads.
    """
    elem = DTYPE_BYTES[kv_dtype]
    total = 0.0
    for g in model.attention:
        tokens = context if g.window is None else min(context, g.window)
        stored_heads = max(g.kv_heads, tp)
        total += 2 * g.layers * stored_heads * g.head_dim * elem * tokens
    return total


def linear_state_bytes(model: ModelSpec, state_dtype: str = "bf16") -> float:
    """Fixed per-sequence state for linear-attention (recurrent) layers."""
    return model.linear_layers * model.linear_elements_per_layer * DTYPE_BYTES[state_dtype]


def concurrent_sequences(users: int, requests_per_hour: float, avg_request_seconds: float) -> int:
    """Little's law: concurrency = arrival rate x time in system.

    `users * requests_per_hour / 3600` is requests per second; multiply by how
    long each request occupies the GPU (prefill + full streamed answer).
    """
    if users < 0 or requests_per_hour < 0 or avg_request_seconds < 0:
        raise ValueError("inputs must be non-negative")
    return math.ceil(users * requests_per_hour / 3600 * avg_request_seconds)


def plan_for_tp(
    model: ModelSpec,
    gpu: GpuSpec,
    tp: int,
    precision: str,
    kv_dtype: str,
    context: int,
    concurrency: int,
    gpu_memory_utilization: float = 0.90,
    overhead_gib: float = 3.0,
) -> Plan:
    """Compute the memory budget for `tp` GPUs serving one model replica.

    vLLM allocates `gpu_memory_utilization` of each GPU. From that it subtracts
    the weight shard and runtime overhead (CUDA context, activations, CUDA
    graphs); what remains becomes paged KV cache shared by all sequences.
    """
    w_gib, estimated = weights_gib(model, precision)
    budget = gpu.memory_gib * gpu_memory_utilization
    per_gpu_w = w_gib / tp
    per_gpu_cache = budget - per_gpu_w - overhead_gib

    seq_bytes = kv_bytes_per_sequence(model, context, kv_dtype, tp) + linear_state_bytes(model)
    seq_gib = seq_bytes / GIB
    # KV heads are split across TP ranks, so cache capacity adds up across GPUs
    # (seq_bytes already includes any replication when tp > kv_heads).
    total_cache = max(per_gpu_cache, 0.0) * tp
    max_seqs = int(total_cache // seq_gib) if seq_gib > 0 else 0

    # Roofline: each decode step streams the active weights once plus every
    # sequence's cache. Aggregate tokens/s <= batch / step_time.
    batch = max(1, min(concurrency, max_seqs))
    active_fraction = model.active_params_billion / model.params_billion
    step_bytes = w_gib * GIB * active_fraction + batch * seq_bytes
    step_seconds = step_bytes / (gpu.bandwidth_gbs * GB * tp)
    ceiling = batch / step_seconds if max_seqs > 0 else 0.0

    return Plan(
        tp=tp,
        weights_gib_total=w_gib,
        weights_estimated=estimated,
        per_gpu_budget_gib=budget,
        per_gpu_weights_gib=per_gpu_w,
        per_gpu_overhead_gib=overhead_gib,
        per_gpu_cache_gib=per_gpu_cache,
        seq_cache_gib=seq_gib,
        max_sequences=max_seqs,
        fits=per_gpu_cache > 0 and max_seqs >= concurrency,
        decode_ceiling_tps=ceiling,
    )


def smallest_plan(plans: list[Plan]) -> Plan | None:
    """First (smallest TP) plan that fits, or None."""
    return next((p for p in plans if p.fits), None)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def render(model: ModelSpec, gpu: GpuSpec, args: argparse.Namespace, plans: list[Plan]) -> str:
    lines = [
        (
            f"Model: {model.name}  precision={args.precision}  kv={args.kv_dtype}  "
            f"GPU: {gpu.name} ({gpu.memory_gib:.1f} GiB, {gpu.bandwidth_gbs:.0f} GB/s)"
        ),
        f"Context per sequence: {args.context} tokens   Target concurrency: {args.concurrency}",
        f"Cache per sequence (TP=1): {plans[0].seq_cache_gib * 1024:.0f} MiB",
    ]
    if plans[0].weights_estimated:
        lines.append(f"NOTE: weights for {args.precision} are ESTIMATED from parameter count.")
    lines.append("")
    lines.append("| TP | Weights/GPU GiB | KV pool/GPU GiB | Max sequences | Fits | Decode ceiling tok/s |")
    lines.append("|---:|---:|---:|---:|:---:|---:|")
    for p in plans:
        lines.append(
            f"| {p.tp} | {p.per_gpu_weights_gib:.1f} | {max(p.per_gpu_cache_gib, 0):.1f} | "
            f"{p.max_sequences} | {'yes' if p.fits else 'no'} | {p.decode_ceiling_tps:,.0f} |"
        )
    best = smallest_plan(plans)
    lines.append("")
    if best:
        lines.append(
            f"Recommendation: {best.tp} x {gpu.name} per replica "
            f"({best.max_sequences} sequences at {args.context} tokens)."
        )
    else:
        lines.append(
            f"Recommendation: no single-node TP degree fits {args.concurrency} sequences; "
            "add replicas, reduce context, or use FP8 KV cache."
        )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)
    p.add_argument("--model", required=True)
    p.add_argument("--precision", default="bf16")
    p.add_argument("--kv-dtype", default="bf16", choices=sorted(DTYPE_BYTES))
    p.add_argument("--gpu", required=True)
    p.add_argument("--context", type=int, default=8192, help="tokens held per active sequence")
    p.add_argument("--concurrency", type=int, default=0, help="concurrent sequences to serve")
    p.add_argument("--users", type=int, default=0)
    p.add_argument("--requests-per-hour", type=float, default=6.0)
    p.add_argument("--avg-request-seconds", type=float, default=25.0)
    p.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    p.add_argument("--overhead-gib", type=float, default=3.0)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    models, gpus = load_catalogue(args.catalogue)
    if args.model not in models:
        print(f"unknown model {args.model!r}; choose from {', '.join(models)}", file=sys.stderr)
        return 2
    if args.gpu not in gpus:
        print(f"unknown gpu {args.gpu!r}; choose from {', '.join(gpus)}", file=sys.stderr)
        return 2
    model, gpu = models[args.model], gpus[args.gpu]
    if args.context > model.max_context:
        print(f"context {args.context} exceeds model maximum {model.max_context}", file=sys.stderr)
        return 2
    if args.users:
        args.concurrency = concurrent_sequences(args.users, args.requests_per_hour, args.avg_request_seconds)
    if args.concurrency <= 0:
        print("set --concurrency or --users", file=sys.stderr)
        return 2

    plans = [
        plan_for_tp(
            model,
            gpu,
            tp,
            args.precision,
            args.kv_dtype,
            args.context,
            args.concurrency,
            args.gpu_memory_utilization,
            args.overhead_gib,
        )
        for tp in TP_DEGREES
    ]
    print(render(model, gpu, args, plans))
    return 0 if smallest_plan(plans) else 1


if __name__ == "__main__":
    raise SystemExit(main())

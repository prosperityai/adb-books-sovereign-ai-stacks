"""Tests for gpu_sizer.py. Run with: pytest tools/sizing -q"""

from __future__ import annotations

import math

import pytest
from gpu_sizer import (
    DEFAULT_CATALOGUE,
    GIB,
    TP_DEGREES,
    concurrent_sequences,
    kv_bytes_per_sequence,
    linear_state_bytes,
    load_catalogue,
    main,
    plan_for_tp,
    smallest_plan,
    weights_gib,
)

MODELS, GPUS = load_catalogue(DEFAULT_CATALOGUE)


def test_catalogue_loads_all_models_and_gpus() -> None:
    assert {"qwen3.8-27b", "gpt-oss-120b", "gemma-4-31b", "gemma-4-26b-a4b"} <= MODELS.keys()
    assert {"l40s", "h100-80g", "h200"} <= GPUS.keys()


def test_qwen_kv_only_counts_full_attention_layers() -> None:
    # 2 (K,V) x 16 layers x 4 heads x 256 dim x 2 bytes = 65,536 bytes per token
    q = MODELS["qwen3.8-27b"]
    assert kv_bytes_per_sequence(q, 1, "bf16") == 65_536
    assert kv_bytes_per_sequence(q, 8192, "bf16") == 65_536 * 8192


def test_fp8_kv_halves_cache() -> None:
    q = MODELS["qwen3.8-27b"]
    assert kv_bytes_per_sequence(q, 4096, "fp8") * 2 == kv_bytes_per_sequence(q, 4096, "bf16")


def test_sliding_window_caps_tokens() -> None:
    g = MODELS["gemma-4-31b"]
    # Beyond the 1024-token window only the 10 global layers keep growing.
    growth = kv_bytes_per_sequence(g, 4096, "bf16") - kv_bytes_per_sequence(g, 2048, "bf16")
    global_per_token = 2 * 10 * 4 * 512 * 2
    assert growth == global_per_token * 2048


def test_linear_state_is_constant_and_positive() -> None:
    q = MODELS["qwen3.8-27b"]
    assert linear_state_bytes(q) == 48 * 817_152 * 2
    assert linear_state_bytes(MODELS["gpt-oss-120b"]) == 0


def test_measured_weights_are_not_estimated() -> None:
    w, est = weights_gib(MODELS["qwen3.8-27b"], "fp8")
    assert not est
    assert math.isclose(w, 30.87e9 / GIB)


def test_unmeasured_precision_is_flagged_as_estimate() -> None:
    _, est = weights_gib(MODELS["gemma-4-31b"], "fp8")
    assert est


def test_unknown_precision_raises() -> None:
    with pytest.raises(ValueError):
        weights_gib(MODELS["gemma-4-31b"], "int2")


def test_littles_law() -> None:
    # 400 users x 6 req/h = 0.667 req/s; x 25 s each = 16.7 -> 17 concurrent
    assert concurrent_sequences(400, 6, 25) == 17
    assert concurrent_sequences(0, 6, 25) == 0
    with pytest.raises(ValueError):
        concurrent_sequences(-1, 6, 25)


def test_tp_beyond_kv_heads_replicates_cache() -> None:
    # Qwen full-attention layers have 4 KV heads; TP=8 stores 8 (2x duplication).
    q = MODELS["qwen3.8-27b"]
    assert kv_bytes_per_sequence(q, 1024, "bf16", tp=8) == 2 * kv_bytes_per_sequence(q, 1024, "bf16", tp=4)
    assert kv_bytes_per_sequence(q, 1024, "bf16", tp=4) == kv_bytes_per_sequence(q, 1024, "bf16", tp=1)


def test_more_gpus_never_reduce_capacity() -> None:
    q, gpu = MODELS["qwen3.8-27b"], GPUS["l40s"]
    caps = [plan_for_tp(q, gpu, tp, "fp8", "fp8", 8192, 32).max_sequences for tp in TP_DEGREES]
    assert caps == sorted(caps)


def test_bf16_gpt_oss_impossible_precision_is_estimated_not_crash() -> None:
    p = plan_for_tp(MODELS["gpt-oss-120b"], GPUS["h100-80g"], 2, "bf16", "bf16", 8192, 10)
    assert p.weights_estimated


def test_gpt_oss_mxfp4_fits_single_h100() -> None:
    p = plan_for_tp(MODELS["gpt-oss-120b"], GPUS["h100-80g"], 1, "mxfp4", "bf16", 8192, 1)
    assert p.fits


def test_smallest_plan_none_when_nothing_fits() -> None:
    g, gpu = MODELS["gemma-4-31b"], GPUS["rtx-4090"]
    plans = [plan_for_tp(g, gpu, tp, "bf16", "bf16", 131072, 500) for tp in TP_DEGREES]
    assert smallest_plan(plans) is None


def test_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--model", "qwen3.8-27b", "--precision", "fp8", "--gpu", "h100-80g", "--concurrency", "8"]) == 0
    assert "Recommendation: 1 x h100-80g" in capsys.readouterr().out
    assert main(["--model", "nope", "--gpu", "l40s", "--concurrency", "1"]) == 2
    assert main(["--model", "qwen3.8-27b", "--gpu", "l40s"]) == 2

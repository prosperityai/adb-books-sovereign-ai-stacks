"""Tests for canary.py: rendering, GPU accounting, gateway overlay and the gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from canary import (
    Side,
    Thresholds,
    decide,
    free_gpus,
    gateway_overlay,
    gpus_requested,
    measure,
    render_canary,
    smoke,
)

REPO = Path(__file__).resolve().parents[2]
DIGEST = "harbor.enclave.internal/models/qwen3.8-27b-fp8@sha256:" + "ab" * 32
STABLE_BASE = "http://vllm-qwen3-8-27b.inference.svc:8000/v1"
CANARY_BASE = "http://vllm-qwen3-8-27b-canary.inference.svc:8000/v1"


def stable_deployment() -> dict:
    docs = list(yaml.safe_load_all((REPO / "platform/inference/vllm-qwen3-8-27b.yaml").read_text()))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    dep["metadata"]["namespace"] = "inference"
    dep["status"] = {"readyReplicas": 1}
    return dep


def test_render_uses_the_chapter5_manifest_and_pins_digest() -> None:
    out = render_canary(stable_deployment(), DIGEST, "40Gi")
    pvc, dep, svc = out["items"]
    assert dep["metadata"]["name"] == "vllm-qwen3-8-27b-canary"
    assert dep["spec"]["selector"] == {"matchLabels": {"app.kubernetes.io/name": "vllm-qwen3-8-27b-canary"}}
    labels = dep["spec"]["template"]["metadata"]["labels"]
    # The network policy selects on component, the gateway on served name: both unchanged.
    assert labels["app.kubernetes.io/component"] == "model-server"
    assert "--served-model-name=qwen3.8-27b" in dep["spec"]["template"]["spec"]["containers"][0]["args"]
    env = dep["spec"]["template"]["spec"]["initContainers"][0]["env"]
    assert {"name": "MODEL_REF", "value": DIGEST} in env
    assert pvc["metadata"]["name"] == "model-cache-qwen3-8-27b-canary"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "40Gi"
    assert svc["spec"]["selector"] == {"app.kubernetes.io/name": "vllm-qwen3-8-27b-canary"}
    assert "status" not in dep and dep["spec"]["replicas"] == 1
    assert gpus_requested(dep) == 1


def test_render_refuses_tags_and_double_canaries() -> None:
    with pytest.raises(ValueError, match="digest"):
        render_canary(stable_deployment(), "harbor.enclave.internal/models/qwen3.8-27b-fp8:017b9c7af6b5", "40Gi")
    canary = render_canary(stable_deployment(), DIGEST, "40Gi")["items"][1]
    with pytest.raises(ValueError, match="already a canary"):
        render_canary(canary, DIGEST, "40Gi")


def test_free_gpus_subtracts_running_requests_only() -> None:
    objs = {
        "items": [
            {"kind": "Node", "metadata": {"name": "gpu-node-1"}, "status": {"allocatable": {"nvidia.com/gpu": "8"}}},
            {
                "kind": "Node",
                "metadata": {"name": "gpu-node-2"},
                "spec": {"unschedulable": True},
                "status": {"allocatable": {"nvidia.com/gpu": "8"}},
            },
            {"kind": "Node", "metadata": {"name": "cp-1"}, "status": {"allocatable": {}}},
            {
                "kind": "Pod",
                "spec": {
                    "nodeName": "gpu-node-1",
                    "containers": [{"resources": {"requests": {"nvidia.com/gpu": "2"}}}],
                },
                "status": {"phase": "Running"},
            },
            {
                "kind": "Pod",
                "spec": {
                    "nodeName": "gpu-node-1",
                    "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}],
                },
                "status": {"phase": "Succeeded"},
            },
        ]
    }
    assert free_gpus(objs) == {"gpu-node-1": 6}


def values() -> dict:
    return yaml.safe_load((REPO / "platform/gateway/litellm-values.yaml").read_text())


def test_overlay_weights_only_the_interactive_alias() -> None:
    ov = gateway_overlay(values(), "qwen3.8-27b", STABLE_BASE, CANARY_BASE, 10)
    models = ov["proxy_config"]["model_list"]
    qwen = [m for m in models if m["model_name"] == "qwen3.8-27b"]
    assert [m["litellm_params"]["weight"] for m in qwen] == [90, 10]
    assert [m["model_info"]["id"] for m in qwen] == ["qwen3.8-27b-stable", "qwen3.8-27b-canary"]
    assert qwen[1]["litellm_params"]["api_base"] == CANARY_BASE
    batch = next(m for m in models if m["model_name"] == "qwen3.8-27b-batch")
    assert batch["litellm_params"]["api_base"] == STABLE_BASE and "weight" not in batch["litellm_params"]
    # Every other model is carried over untouched (Helm replaces the whole list).
    assert {m["model_name"] for m in models} == {
        "qwen3.8-27b",
        "qwen3.8-27b-batch",
        "gpt-oss-120b",
        "bge-m3",
        "qwen3.8-27b-candidate",
    }


def test_overlay_at_100_moves_every_stable_deployment() -> None:
    models = gateway_overlay(values(), "qwen3.8-27b", STABLE_BASE, CANARY_BASE, 100)["proxy_config"]["model_list"]
    bases = {m["model_name"]: m["litellm_params"]["api_base"] for m in models}
    assert bases["qwen3.8-27b"] == CANARY_BASE and bases["qwen3.8-27b-batch"] == CANARY_BASE
    assert bases["gpt-oss-120b"].startswith("http://vllm-gpt-oss-120b")
    assert bases["qwen3.8-27b-candidate"] == CANARY_BASE
    assert len(models) == 5


def test_overlay_at_0_only_adds_the_candidate_alias_and_bad_input_fails() -> None:
    base = values()["proxy_config"]["model_list"]
    models = gateway_overlay(values(), "qwen3.8-27b", STABLE_BASE, CANARY_BASE, 0)["proxy_config"]["model_list"]
    assert models[:-1] == base
    cand = models[-1]
    assert cand["model_name"] == "qwen3.8-27b-candidate" and cand["litellm_params"]["api_base"] == CANARY_BASE
    assert cand["litellm_params"]["model"] == "hosted_vllm/qwen3.8-27b"  # the served name is unchanged
    with pytest.raises(ValueError):
        gateway_overlay(values(), "qwen3.8-27b", STABLE_BASE, CANARY_BASE, 101)
    with pytest.raises(ValueError, match="not found"):
        gateway_overlay(values(), "no-such-model", STABLE_BASE, CANARY_BASE, 10)


def test_smoke_flags_empty_and_failed_answers() -> None:
    answers = iter(
        [
            {"choices": [{"finish_reason": "stop", "message": {"content": "Dodoma, Arusha na Mwanza."}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
            RuntimeError("HTTP Error 500"),
        ]
    )

    def post(url: str, body: dict, headers: dict) -> dict:
        assert url == "http://x:8000/v1/chat/completions" and headers["Authorization"] == "Bearer k"
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    failures = smoke("http://x:8000/v1/", "qwen3.8-27b", "k", post)
    assert len(failures) == 2
    assert failures[0].startswith("en: empty") and failures[1].startswith("ar: request failed")


T = Thresholds()
GOOD = Side(requests=1800, aborts=9, ttft_p95=0.62, e2e_p95=7.9)


def test_gate_promotes_a_comparable_canary() -> None:
    verdict, reasons = decide(GOOD, Side(requests=210, aborts=1, ttft_p95=0.70, e2e_p95=8.4), T)
    assert verdict == "promote", reasons


def test_gate_waits_for_traffic_and_catches_a_cooled_down_canary() -> None:
    verdict, reasons = decide(GOOD, Side(requests=12, aborts=0, ttft_p95=0.5, e2e_p95=6.0), T)
    assert verdict == "wait" and "need 200" in reasons[0]
    assert decide(GOOD, Side(requests=500, aborts=0, ttft_p95=None, e2e_p95=None), T)[0] == "wait"


def test_gate_rolls_back_on_latency_slo_or_aborts() -> None:
    slow = Side(requests=400, aborts=2, ttft_p95=0.9, e2e_p95=8.0)
    verdict, reasons = decide(GOOD, slow, T)
    assert verdict == "rollback" and "TTFT p95 0.90s vs stable 0.62s" in reasons[0]
    over_slo = Side(requests=400, aborts=2, ttft_p95=3.4, e2e_p95=8.0)
    assert any("exceeds SLO" in r for r in decide(Side(1800, 9, 3.3, 7.9), over_slo, T)[1])
    aborting = Side(requests=400, aborts=40, ttft_p95=0.6, e2e_p95=8.0)
    assert decide(GOOD, aborting, T) == ("rollback", ["abort ratio 0.100 vs stable 0.005"])


def test_measure_builds_one_query_per_signal() -> None:
    seen: list[str] = []

    def q(expr: str) -> float | None:
        seen.append(expr)
        return None if "histogram_quantile" in expr else 5.0

    side = measure(q, 'namespace="inference",pod=~"vllm-qwen3-8-27b-canary-.*"', "15m")
    assert side == Side(requests=5.0, aborts=5.0, ttft_p95=None, e2e_p95=None)
    assert len(seen) == 4 and all('pod=~"vllm-qwen3-8-27b-canary-.*"' in e for e in seen)
    assert 'finished_reason="abort"' in seen[1] and "[15m]" in seen[2]
    json.dumps(seen)  # every query is plain text, safe to log

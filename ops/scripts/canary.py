#!/usr/bin/env python3
r"""canary.py — the decision logic behind blue/green model upgrades.

Book: Sovereign AI Stacks, Chapter 12
Requires: Python 3.13; PyYAML 6.0 for `overlay` only. `smoke` runs inside the
          LiteLLM 1.106 pod with the standard library alone.

upgrade_model.sh calls one subcommand per step and keeps no logic of its own:

  render   stable Deployment JSON (stdin) -> canary PVC + Deployment + Service
           (JSON List on stdout), pinned to a new model reference by digest
  gpu-free `kubectl get nodes,pods -A -o json` (stdin) -> free GPUs per node
  overlay  litellm-values.yaml -> a values overlay that exposes the canary as
           <alias>-candidate and gives it a share of the production alias
  smoke    three short prompts in Swahili, English and Arabic, sent straight to
           a model server; fails on anything but a sane completion
  gate     compares canary and stable in Prometheus and prints a verdict

Exit codes for `gate`: 0 promote, 1 roll back, 2 not enough evidence yet.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass

CANARY_SUFFIX = "-canary"
DIGEST_REF = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
MODEL_REF_ENV = "MODEL_REF"
ANNOTATION_REF = "sovereign-ai/model-ref"
ANNOTATION_PREVIOUS = "sovereign-ai/previous-model-ref"


# --- render ------------------------------------------------------------------
def _clean_metadata(meta: dict, name: str) -> dict:
    keep = {"name": name, "labels": dict(meta.get("labels", {}))}
    if meta.get("namespace"):
        keep["namespace"] = meta["namespace"]
    return keep


def render_canary(stable: dict, model_ref: str, cache_size: str) -> dict:
    """Build the green copy of a stable vLLM Deployment, with its own PVC and Service.

    The copy keeps the served model name, GPU requests, probes and security
    context, so the gateway can send it the same requests. Only names, the
    selector label, the cache volume and MODEL_REF change.
    """
    if not DIGEST_REF.match(model_ref):
        raise ValueError(f"canary must be pinned by digest (registry/repo@sha256:<64 hex>), got {model_ref!r}")
    name = stable["metadata"]["name"]
    if name.endswith(CANARY_SUFFIX):
        raise ValueError(f"{name} is already a canary")
    cname = name + CANARY_SUFFIX
    dep = copy.deepcopy(stable)
    dep.pop("status", None)
    dep["metadata"] = _clean_metadata(stable["metadata"], cname)
    dep["metadata"]["labels"]["app.kubernetes.io/name"] = cname
    dep["metadata"]["labels"]["sovereign-ai/track"] = "canary"
    dep["metadata"]["annotations"] = {ANNOTATION_REF: model_ref}
    spec = dep["spec"]
    spec["replicas"] = 1
    spec["selector"] = {"matchLabels": {"app.kubernetes.io/name": cname}}
    tmpl = spec["template"]
    tmpl.setdefault("metadata", {}).setdefault("labels", {})
    tmpl["metadata"]["labels"]["app.kubernetes.io/name"] = cname
    tmpl["metadata"]["labels"]["sovereign-ai/track"] = "canary"
    tmpl["metadata"].pop("annotations", None)

    pod = tmpl["spec"]
    found = False
    for c in pod.get("initContainers", []):
        for env in c.get("env", []):
            if env.get("name") == MODEL_REF_ENV:
                env["value"] = model_ref
                found = True
    if not found:
        raise ValueError(f"{name} has no init container with {MODEL_REF_ENV}; is it a Chapter 5 model server?")

    pvc_name = None
    for vol in pod.get("volumes", []):
        if vol.get("name") == "models" and "persistentVolumeClaim" in vol:
            pvc_name = vol["persistentVolumeClaim"]["claimName"] + CANARY_SUFFIX
            vol["persistentVolumeClaim"]["claimName"] = pvc_name
    if pvc_name is None:
        raise ValueError(f"{name} has no 'models' PVC volume")

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name, "labels": {"sovereign-ai/track": "canary"}},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "ceph-block",
            "resources": {"requests": {"storage": cache_size}},
        },
    }
    if stable["metadata"].get("namespace"):
        pvc["metadata"]["namespace"] = stable["metadata"]["namespace"]
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": cname,
            "labels": {
                "app.kubernetes.io/name": cname,
                "app.kubernetes.io/component": "model-server",
                "sovereign-ai/track": "canary",
            },
        },
        "spec": {
            "selector": {"app.kubernetes.io/name": cname},
            "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
        },
    }
    if stable["metadata"].get("namespace"):
        svc["metadata"]["namespace"] = stable["metadata"]["namespace"]
    return {"apiVersion": "v1", "kind": "List", "items": [pvc, dep, svc]}


def gpus_requested(dep: dict) -> int:
    total = 0
    for c in dep["spec"]["template"]["spec"].get("containers", []):
        total += int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
    return total


# --- gpu-free ----------------------------------------------------------------
def free_gpus(objects: dict) -> dict[str, int]:
    """Allocatable minus requested nvidia.com/gpu per schedulable node."""
    free: dict[str, int] = {}
    for item in objects.get("items", []):
        if item.get("kind") != "Node" or item.get("spec", {}).get("unschedulable"):
            continue
        alloc = int(item.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", 0))
        if alloc:
            free[item["metadata"]["name"]] = alloc
    for item in objects.get("items", []):
        if item.get("kind") != "Pod" or item.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        node = item.get("spec", {}).get("nodeName")
        if node not in free:
            continue
        for c in item["spec"].get("containers", []):
            free[node] -= int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
    return free


# --- overlay -----------------------------------------------------------------
CANDIDATE_SUFFIX = "-candidate"


def gateway_overlay(values: dict, alias: str, stable_base: str, canary_base: str, weight: int) -> dict:
    """Return a Helm overlay that exposes the canary to the gateway.

    While a canary exists it is ALWAYS reachable under its own alias,
    `<alias>-candidate` (qwen3.8-27b-candidate), which only the eval harness
    key may call: Chapter 11's acceptance run targets that name. `weight` then
    decides how much of the production alias it also serves:
      0       none: candidate alias only (just staged, or under evaluation)
      1-99    that percentage of `alias`, by LiteLLM's weighted simple-shuffle
      100     every deployment on the stable backend, the batch alias too, so
              the stable Deployment can be recreated during promotion
    Helm replaces lists rather than merging them, so the overlay carries the
    whole model_list.
    """
    if not 0 <= weight <= 100:
        raise ValueError("weight must be between 0 and 100")
    models = copy.deepcopy(values["proxy_config"]["model_list"])
    template = next((m for m in models if m["model_name"] == alias), None)
    if template is None:
        raise ValueError(f"alias {alias!r} not found in model_list")
    out: list[dict] = []
    for m in models:
        params = m["litellm_params"]
        on_stable = params.get("api_base", "").rstrip("/") == stable_base.rstrip("/")
        if weight == 100 and on_stable:
            moved = copy.deepcopy(m)
            moved["litellm_params"]["api_base"] = canary_base
            moved.setdefault("model_info", {})["id"] = f"{m['model_name']}-canary"
            out.append(moved)
        elif 0 < weight < 100 and on_stable and m["model_name"] == alias:
            stable = copy.deepcopy(m)
            stable["litellm_params"]["weight"] = 100 - weight
            stable.setdefault("model_info", {})["id"] = f"{alias}-stable"
            canary = copy.deepcopy(m)
            canary["litellm_params"]["api_base"] = canary_base
            canary["litellm_params"]["weight"] = weight
            canary.setdefault("model_info", {})["id"] = f"{alias}-canary"
            out.extend([stable, canary])
        else:
            out.append(m)
    candidate = copy.deepcopy(template)
    candidate["model_name"] = alias + CANDIDATE_SUFFIX
    candidate["litellm_params"]["api_base"] = canary_base
    candidate["litellm_params"].pop("weight", None)
    candidate.setdefault("model_info", {})["id"] = alias + CANDIDATE_SUFFIX
    out.append(candidate)
    return {"proxy_config": {"model_list": out}}


# --- smoke -------------------------------------------------------------------
SMOKE_PROMPTS = [
    ("sw", "Taja mikoa mitatu ya Tanzania kwa sentensi moja."),
    ("en", "In one sentence, what does a ministry of health do?"),
    ("ar", "اذكر عاصمة دولة الإمارات العربية المتحدة في جملة واحدة."),
]


def smoke(base_url: str, model: str, api_key: str, post: Callable[[str, dict, dict], dict] | None = None) -> list[str]:
    """Send one prompt per language; return a list of failures (empty means pass)."""
    post = post or _http_post
    failures = []
    for lang, prompt in SMOKE_PROMPTS:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 128}
        try:
            resp = post(f"{base_url.rstrip('/')}/chat/completions", body, {"Authorization": f"Bearer {api_key}"})
        except Exception as exc:  # noqa: BLE001 — any transport error is a smoke failure
            failures.append(f"{lang}: request failed: {exc}")
            continue
        choice = (resp.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") not in ("stop", "length"):
            failures.append(f"{lang}: finish_reason={choice.get('finish_reason')!r}")
        elif len(text.strip()) < 5:
            failures.append(f"{lang}: empty or truncated answer {text!r}")
    return failures


def _http_post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(  # noqa: S310 — in-cluster http URL built by the caller
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return json.load(resp)


# --- gate --------------------------------------------------------------------
@dataclass(frozen=True)
class Thresholds:
    min_requests: int = 200
    ttft_p95_ratio: float = 1.25
    e2e_p95_ratio: float = 1.25
    ttft_p95_slo_s: float = 3.0
    abort_delta: float = 0.02


@dataclass(frozen=True)
class Side:
    requests: float
    aborts: float
    ttft_p95: float | None
    e2e_p95: float | None

    @property
    def abort_ratio(self) -> float:
        return self.aborts / self.requests if self.requests else 0.0


def queries(selector: str, window: str) -> dict[str, str]:
    """PromQL for one side. `selector` is a label matcher list without braces."""
    return {
        "requests": f"sum(increase(vllm:request_success_total{{{selector}}}[{window}]))",
        "aborts": f'sum(increase(vllm:request_success_total{{{selector},finished_reason="abort"}}[{window}]))',
        "ttft_p95": "histogram_quantile(0.95, sum by (le) "
        f"(rate(vllm:time_to_first_token_seconds_bucket{{{selector}}}[{window}])))",
        "e2e_p95": "histogram_quantile(0.95, sum by (le) "
        f"(rate(vllm:e2e_request_latency_seconds_bucket{{{selector}}}[{window}])))",
    }


def measure(query: Callable[[str], float | None], selector: str, window: str) -> Side:
    q = queries(selector, window)
    return Side(
        requests=query(q["requests"]) or 0.0,
        aborts=query(q["aborts"]) or 0.0,
        ttft_p95=query(q["ttft_p95"]),
        e2e_p95=query(q["e2e_p95"]),
    )


def decide(stable: Side, canary: Side, t: Thresholds) -> tuple[str, list[str]]:
    """Return ('promote' | 'rollback' | 'wait', reasons)."""
    if canary.requests < t.min_requests:
        # A canary the router has put in cooldown after errors also lands here:
        # LiteLLM retries its failures on the stable deployment, so the only
        # visible symptom is that the canary stops receiving traffic.
        return "wait", [f"canary served {canary.requests:.0f} requests, need {t.min_requests}"]
    if canary.ttft_p95 is None or canary.e2e_p95 is None:
        return "wait", ["canary latency histograms are empty"]
    reasons = []
    if canary.ttft_p95 > t.ttft_p95_slo_s:
        reasons.append(f"canary TTFT p95 {canary.ttft_p95:.2f}s exceeds SLO {t.ttft_p95_slo_s:.2f}s")
    if stable.ttft_p95 and canary.ttft_p95 > stable.ttft_p95 * t.ttft_p95_ratio:
        reasons.append(f"TTFT p95 {canary.ttft_p95:.2f}s vs stable {stable.ttft_p95:.2f}s (> x{t.ttft_p95_ratio})")
    if stable.e2e_p95 and canary.e2e_p95 > stable.e2e_p95 * t.e2e_p95_ratio:
        reasons.append(f"E2E p95 {canary.e2e_p95:.2f}s vs stable {stable.e2e_p95:.2f}s (> x{t.e2e_p95_ratio})")
    if canary.abort_ratio > stable.abort_ratio + t.abort_delta:
        reasons.append(f"abort ratio {canary.abort_ratio:.3f} vs stable {stable.abort_ratio:.3f}")
    if reasons:
        return "rollback", reasons
    summary = (
        f"{canary.requests:.0f} requests; TTFT p95 {canary.ttft_p95:.2f}s; E2E p95 {canary.e2e_p95:.2f}s; "
        f"abort ratio {canary.abort_ratio:.3f}"
    )
    return "promote", [summary]


def prometheus_query(base_url: str) -> Callable[[str], float | None]:
    def run(expr: str) -> float | None:
        url = f"{base_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — operator-supplied Prometheus URL
            body = json.load(resp)
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus error for {expr!r}: {body.get('error')}")
        result = body["data"]["result"]
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if value != value else value  # NaN from an empty histogram

    return run


# --- CLI ---------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="stable Deployment JSON on stdin -> canary objects on stdout")
    r.add_argument("--model-ref", required=True)
    r.add_argument("--cache-size", required=True, help="size of the stable model-cache PVC, e.g. 40Gi")

    sub.add_parser("gpu-free", help="nodes+pods JSON on stdin -> free GPUs per node")

    o = sub.add_parser("overlay", help="write a LiteLLM values overlay")
    o.add_argument("--values", required=True)
    o.add_argument("--alias", required=True)
    o.add_argument("--stable-base", required=True)
    o.add_argument("--canary-base", required=True)
    o.add_argument("--weight", type=int, required=True)

    s = sub.add_parser("smoke", help="three prompts straight to a model server")
    s.add_argument("--base-url", required=True)
    s.add_argument("--model", required=True)

    g = sub.add_parser("gate", help="compare canary and stable in Prometheus")
    g.add_argument("--prometheus", default=os.environ.get("PROM_URL", "http://127.0.0.1:9090"))
    g.add_argument("--stable-selector", required=True)
    g.add_argument("--canary-selector", required=True)
    g.add_argument("--window", default="15m")
    g.add_argument("--min-requests", type=int, default=Thresholds.min_requests)
    g.add_argument("--ttft-slo", type=float, default=Thresholds.ttft_p95_slo_s)

    a = p.parse_args(argv)
    if a.cmd == "render":
        stable = json.load(sys.stdin)
        objs = render_canary(stable, a.model_ref, a.cache_size)
        print(json.dumps({**objs, "metadata": {"gpus": gpus_requested(stable)}}, indent=2))
        return 0
    if a.cmd == "gpu-free":
        for node, n in sorted(free_gpus(json.load(sys.stdin)).items()):
            print(f"{node} {n}")
        return 0
    if a.cmd == "overlay":
        import yaml

        with open(a.values, encoding="utf-8") as f:
            values = yaml.safe_load(f)
        overlay = gateway_overlay(values, a.alias, a.stable_base, a.canary_base, a.weight)
        sys.stdout.write("# Generated by ops/scripts/canary.py overlay. Do not edit.\n")
        sys.stdout.write(yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True))
        return 0
    if a.cmd == "smoke":
        failures = smoke(a.base_url, a.model, os.environ.get("VLLM_API_KEY", ""))
        for f in failures:
            print(f"SMOKE FAIL {f}")
        if not failures:
            print(f"SMOKE PASS {len(SMOKE_PROMPTS)} prompts (sw, en, ar)")
        return 1 if failures else 0
    # gate
    t = Thresholds(min_requests=a.min_requests, ttft_p95_slo_s=a.ttft_slo)
    query = prometheus_query(a.prometheus)
    stable = measure(query, a.stable_selector, a.window)
    canary = measure(query, a.canary_selector, a.window)
    verdict, reasons = decide(stable, canary, t)
    print(json.dumps({"verdict": verdict, "reasons": reasons, "stable": asdict(stable), "canary": asdict(canary)}))
    return {"promote": 0, "rollback": 1, "wait": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())

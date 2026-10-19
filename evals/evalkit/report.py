"""Aggregate a run per language and per suite, price it, and write the scorecard.

Book: Sovereign AI Stacks, Chapter 11
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

REDTEAM_KIND = "redteam"


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: always a value that was actually observed."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def price(rec: dict, model: str, pricing: dict) -> float:
    models = pricing.get("models", {})
    rate = models.get(model) or models.get("default") or {}
    return (
        rec["prompt_tokens"] / 1000 * float(rate.get("prompt_per_1k", 0))
        + rec["completion_tokens"] / 1000 * float(rate.get("completion_per_1k", 0))
    )


def _rate(recs: list[dict], check: str) -> float | None:
    scored = [r for r in recs if check in r["checks"]]
    return round(sum(r["checks"][check] for r in scored) / len(scored), 4) if scored else None


def stats(recs: list[dict], model: str, pricing: dict) -> dict:
    n = len(recs)
    lat = [r["latency_s"] for r in recs if r["error"] is None]
    answerable = [r for r in recs if r["kind"] in ("answer", "short")]
    abstain = [r for r in recs if r["kind"] == "abstain"]
    hits = [r["retrieval_hit"] for r in recs if r["retrieval_hit"] is not None]
    judged = [r["judge_score"] for r in recs if r["judge_score"] is not None]
    costs = [price(r, model, pricing) for r in recs]
    return {
        "items": n,
        "correct": sum(r["correct"] for r in recs),
        "accuracy": round(sum(r["correct"] for r in recs) / n, 4) if n else 0.0,
        "citation_rate": _rate(recs, "citations"),
        "language_match": _rate(recs, "language"),
        "abstain_accuracy": round(sum(r["correct"] for r in abstain) / len(abstain), 4) if abstain else None,
        "over_refusals": sum(1 for r in answerable if r["checks"].get("not_refused") is False),
        "errors": sum(1 for r in recs if r["error"]),
        "retries": sum(r["retries"] for r in recs),
        "cached": sum(1 for r in recs if r.get("cached")),
        "latency_p50_s": round(percentile(lat, 50), 3),
        "latency_p95_s": round(percentile(lat, 95), 3),
        "mean_prompt_tokens": round(sum(r["prompt_tokens"] for r in recs) / n, 1) if n else 0.0,
        "mean_completion_tokens": round(sum(r["completion_tokens"] for r in recs) / n, 1) if n else 0.0,
        "mean_cost": round(sum(costs) / n, 6) if n else 0.0,
        "retrieval_recall": round(sum(hits) / len(hits), 4) if hits else None,
        "judge_mean": round(sum(judged) / len(judged), 2) if judged else None,
    }


def summarise(run: dict, pricing: dict) -> dict:
    model = run["model"]
    qa = [r for r in run["results"] if r["kind"] != REDTEAM_KIND]
    rt = [r for r in run["results"] if r["kind"] == REDTEAM_KIND]
    by_lang: dict[str, list[dict]] = defaultdict(list)
    by_suite: dict[str, list[dict]] = defaultdict(list)
    for r in qa:
        by_lang[r["lang"]].append(r)
        by_suite[r["suite"]].append(r)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rt:
        by_cat[r["category"]].append(r)
    return {
        "overall": stats(qa, model, pricing),
        "per_language": {k: stats(v, model, pricing) for k, v in sorted(by_lang.items())},
        "per_suite": {k: stats(v, model, pricing) for k, v in sorted(by_suite.items())},
        "redteam": {
            "cases": len(rt),
            "passed": sum(r["correct"] for r in rt),
            "pass_rate": round(sum(r["correct"] for r in rt) / len(rt), 4) if rt else None,
            "per_category": {
                k: {"cases": len(v), "passed": sum(r["correct"] for r in v)} for k, v in sorted(by_cat.items())
            },
            "failures": [{"id": r["id"], "checks": failed_checks(r)} for r in rt if not r["correct"]],
        },
        "currency": pricing.get("currency", ""),
    }


def failed_checks(rec: dict) -> list[str]:
    return [k for k, ok in rec["checks"].items() if not ok] or (["error"] if rec["error"] else [])


def _fmt(v: object, pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if pct and isinstance(v, float | int):
        return f"{v * 100:.1f}%"
    return str(v)


def scorecard_markdown(run: dict) -> str:
    s = run["summary"]
    cur = s["currency"]
    datasets = ", ".join(f"{name} ({d['sha256'][:12]})" for name, d in run["datasets"].items())
    lines = [
        f"# Scorecard: {run['model']}",
        "",
        f"- Run: `{run['run_id']}`  target: `{run['qa_target']}`  harness {run['harness_version']}",
        f"- Started {run['started_at']}, finished {run['finished_at']}, concurrency {run['concurrency']}",
        f"- Datasets: {datasets}",
        "",
        "## Golden sets",
        "",
        f"Cost is the mean internal chargeback per answer, in {cur or 'unpriced units'}.",
        "",
        "| Lang | Items | Accuracy | Citations | Language | Abstain | Refused | Errors | p50 s | p95 s | Cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows = [*s["per_language"].items(), ("all", s["overall"])]
    for lang, st in rows:
        lines.append(
            f"| {lang} | {st['items']} | {_fmt(st['accuracy'], True)} | {_fmt(st['citation_rate'], True)} | "
            f"{_fmt(st['language_match'], True)} | {_fmt(st['abstain_accuracy'], True)} | {st['over_refusals']} | "
            f"{st['errors']} | {st['latency_p50_s']} | {st['latency_p95_s']} | {st['mean_cost']:.6f} |"
        )
    if any(st["retrieval_recall"] is not None for _, st in rows):
        lines += ["", "Retrieval recall (required section in RAG citations): "
                  + ", ".join(f"{k} {_fmt(st['retrieval_recall'], True)}" for k, st in rows)]
    if any(st["judge_mean"] is not None for _, st in rows):
        lines += ["", f"Judge (advisory, {run['judge_model']}): "
                  + ", ".join(f"{k} {st['judge_mean']}" for k, st in rows)]
    rt = s["redteam"]
    lines += ["", "## Red team", "", f"{rt['passed']}/{rt['cases']} cases passed.", "",
              "| Category | Cases | Passed |", "|---|---|---|"]
    lines += [f"| {k} | {v['cases']} | {v['passed']} |" for k, v in rt["per_category"].items()]
    fails = [r for r in run["results"] if not r["correct"]]
    if fails:
        lines += ["", "## Failures", "", "| Item | Lang | Failed checks |", "|---|---|---|"]
        lines += [f"| {r['id']} | {r['lang']} | {', '.join(failed_checks(r))} |" for r in fails]
    return "\n".join(lines) + "\n"


def write_run(run: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "scorecard.md").write_text(scorecard_markdown(run), encoding="utf-8")

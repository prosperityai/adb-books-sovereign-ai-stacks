"""Acceptance gates: a candidate run against criteria.yaml and, optionally, a baseline run.

Book: Sovereign AI Stacks, Chapter 11

Every gate is evaluated and reported, even after the first failure, because the
person reading the sign-off report needs the whole picture, not the first
problem. The verdict is ACCEPT only when every gate passes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from evalkit.report import failed_checks


@dataclass(frozen=True)
class GateResult:
    gate: str
    scope: str
    value: float | int | str | None
    threshold: float | int | str | None
    passed: bool
    detail: str = ""


def _floor(gate: str, scope: str, value: float | None, minimum: float | None) -> GateResult | None:
    if minimum is None:
        return None
    if value is None:
        return GateResult(gate, scope, None, minimum, False, "not measured: no items exercise this check")
    return GateResult(gate, scope, value, minimum, value >= minimum)


def evaluate(candidate: dict, criteria: dict, baseline: dict | None = None) -> list[GateResult]:
    out: list[GateResult] = []
    cs = candidate["summary"]

    if baseline is not None:
        same = {k: v["sha256"] for k, v in candidate["datasets"].items()} == {
            k: v["sha256"] for k, v in baseline["datasets"].items()
        }
        out.append(
            GateResult(
                "same-datasets",
                "all",
                "match" if same else "differ",
                "match",
                same,
                "" if same else "baseline answered different questions: re-run the baseline",
            )
        )

    min_items = int(criteria.get("min_items_per_language", 1))
    for lang, floors in criteria.get("languages", {}).items():
        st = cs["per_language"].get(lang)
        if st is None:
            out.append(GateResult("coverage", lang, 0, min_items, False, "no items for this language"))
            continue
        out.append(GateResult("coverage", lang, st["items"], min_items, st["items"] >= min_items))
        for metric in ("accuracy", "citation_rate", "language_match", "abstain_accuracy"):
            g = _floor(f"min-{metric}", lang, st[metric], floors.get(f"min_{metric}"))
            if g:
                out.append(g)
        p95 = criteria.get("latency", {}).get("p95_seconds")
        if p95 is not None:
            out.append(GateResult("latency-p95", lang, st["latency_p95_s"], p95, st["latency_p95_s"] <= p95))

        if baseline is not None:
            bs = baseline["summary"]["per_language"].get(lang)
            for metric, max_drop in criteria.get("regression", {}).items():
                if bs is None or bs.get(metric) is None or st.get(metric) is None:
                    continue
                drop = round(bs[metric] - st[metric], 4)
                out.append(
                    GateResult(
                        f"regression-{metric}",
                        lang,
                        drop,
                        max_drop,
                        drop <= max_drop,
                        f"baseline {bs[metric]:.3f} -> candidate {st[metric]:.3f}",
                    )
                )

    total = cs["overall"]["items"] + (cs["redteam"]["cases"] or 0)
    errors = cs["overall"]["errors"] + sum(1 for r in candidate["results"] if r["kind"] == "redteam" and r["error"])
    max_err = criteria.get("errors", {}).get("max_error_rate")
    if max_err is not None and total:
        rate = round(errors / total, 4)
        out.append(GateResult("error-rate", "all", rate, max_err, rate <= max_err, f"{errors} of {total} calls failed"))

    rt_crit = criteria.get("redteam", {})
    rt = cs["redteam"]
    if "min_pass_rate" in rt_crit:
        rate = rt["pass_rate"] if rt["pass_rate"] is not None else 0.0
        out.append(
            GateResult(
                "redteam-pass-rate",
                "all",
                rate,
                rt_crit["min_pass_rate"],
                rt["cases"] > 0 and rate >= rt_crit["min_pass_rate"],
                ", ".join(f["id"] for f in rt["failures"]),
            )
        )
    for cat in rt_crit.get("required_categories", []):
        n = rt["per_category"].get(cat, {}).get("cases", 0)
        out.append(GateResult("redteam-category", cat, n, 1, n >= 1, "" if n else "category not exercised"))

    cost = criteria.get("cost", {})
    if baseline is not None and cost.get("max_increase") is not None:
        b, c = baseline["summary"]["overall"]["mean_cost"], cs["overall"]["mean_cost"]
        if b > 0:
            inc = round((c - b) / b, 4)
            out.append(
                GateResult(
                    "cost-increase",
                    "all",
                    inc,
                    cost["max_increase"],
                    inc <= cost["max_increase"],
                    f"{b:.6f} -> {c:.6f} {cost.get('currency', '')} per answer",
                )
            )

    judge_min = criteria.get("judge", {}).get("min_mean")
    if judge_min is not None:
        for lang, st in cs["per_language"].items():
            g = _floor("judge-mean", lang, st["judge_mean"], judge_min)
            if g:
                out.append(g)
    return out


def item_diff(candidate: dict, baseline: dict) -> dict[str, dict[str, list[dict]]]:
    """Per language: items that passed in the baseline and fail now, and the reverse."""
    base = {r["id"]: r for r in baseline["results"]}
    diff: dict[str, dict[str, list[dict]]] = {}
    for r in candidate["results"]:
        b = base.get(r["id"])
        if b is None or b["correct"] == r["correct"]:
            continue
        bucket = diff.setdefault(r["lang"], {"regressed": [], "fixed": []})
        entry = {"id": r["id"], "checks": failed_checks(r if b["correct"] else b), "answer": r["answer"]}
        bucket["regressed" if b["correct"] else "fixed"].append(entry)
    return diff


def gate_markdown(candidate: dict, baseline: dict | None, criteria: dict, results: list[GateResult]) -> str:
    failed = [g for g in results if not g.passed]
    verdict = "ACCEPT" if not failed else "REJECT"
    lines = [
        f"# Acceptance report: {candidate['model']}",
        "",
        f"- Candidate run: `{candidate['run_id']}` ({candidate['finished_at']})",
        f"- Baseline run: `{baseline['run_id']}`" if baseline else "- Baseline run: none (first acceptance)",
        f"- Criteria version: {criteria.get('version', 'unversioned')}",
        "",
        f"**Verdict: {verdict}** ({len(results) - len(failed)} of {len(results)} gates passed)",
        "",
        "| Gate | Scope | Value | Threshold | Result | Detail |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {g.gate} | {g.scope} | {g.value} | {g.threshold} | {'PASS' if g.passed else '**FAIL**'} | {g.detail} |"
        for g in results
    ]
    if baseline:
        diff = item_diff(candidate, baseline)
        if diff:
            lines += ["", "## Changed items", "", "| Lang | Item | Change | Failed checks |", "|---|---|---|---|"]
            for lang in sorted(diff):
                for kind in ("regressed", "fixed"):
                    lines += [f"| {lang} | {e['id']} | {kind} | {', '.join(e['checks'])} |" for e in diff[lang][kind]]
    lines += [
        "",
        "## Sign-off",
        "",
        "| Role | Name | Signature | Date |",
        "|---|---|---|---|",
        "| Platform owner (integrator) | | | |",
        "| Client technical authority | | | |",
        "| Client language reviewer (sw/ar) | | | |",
    ]
    return "\n".join(lines) + "\n"


def write_gate(candidate: dict, baseline: dict | None, criteria: dict, results: list[GateResult], out: Path) -> bool:
    out.mkdir(parents=True, exist_ok=True)
    passed = all(g.passed for g in results)
    doc = {
        "verdict": "ACCEPT" if passed else "REJECT",
        "candidate": candidate["run_id"],
        "baseline": baseline["run_id"] if baseline else None,
        "criteria_version": criteria.get("version"),
        "gates": [asdict(g) for g in results],
        "changed_items": item_diff(candidate, baseline) if baseline else {},
    }
    (out / "gate.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "gate.md").write_text(gate_markdown(candidate, baseline, criteria, results), encoding="utf-8")
    return passed

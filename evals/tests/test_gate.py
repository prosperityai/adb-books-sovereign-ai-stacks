"""The chapter's war story as a test: English improves, Swahili regresses, the gate says no.

Book: Sovereign AI Stacks, Chapter 11
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import failed
from evalkit.mock import EVALS_DIR


def test_swahili_regression_fails_gate_while_english_improves(run_model, gate):
    _, base, base_path = run_model("qwen3.8-27b")
    _, cand, cand_path = run_model("qwen3.8-27b-candidate")
    b, c = base["summary"], cand["summary"]

    # The headline number does not move, and English gets better...
    assert c["overall"]["accuracy"] == b["overall"]["accuracy"]
    assert c["per_language"]["en"]["accuracy"] > b["per_language"]["en"]["accuracy"]
    # ...while Swahili gets worse, landing exactly on its absolute floor.
    assert c["per_language"]["sw"]["accuracy"] < b["per_language"]["sw"]["accuracy"]
    assert c["per_language"]["sw"]["accuracy"] == 0.8

    code, doc = gate(cand_path, base_path)
    assert code == 1
    assert doc["verdict"] == "REJECT"
    assert failed(doc) == {
        ("regression-accuracy", "sw"),
        ("regression-language_match", "sw"),
        ("min-language_match", "sw"),
    }
    # A floor-only gate would have promoted it.
    assert ("min-accuracy", "sw") not in failed(doc)
    regressed = {e["id"] for e in doc["changed_items"]["sw"]["regressed"]}
    assert regressed == {"sw-003", "sw-005", "sw-011"}
    assert {e["id"] for e in doc["changed_items"]["en"]["fixed"]} == {"en-009", "en-012"}


def test_baseline_against_itself_passes(run_model, gate):
    _, _, first = run_model("qwen3.8-27b", name="b1")
    _, _, second = run_model("qwen3.8-27b", name="b2")
    code, doc = gate(second, first)
    assert code == 0, failed(doc)
    assert doc["verdict"] == "ACCEPT"


def test_first_acceptance_without_baseline(run_model, gate):
    _, _, path = run_model("qwen3.8-27b")
    code, doc = gate(path)
    assert code == 0
    assert not any(g["gate"].startswith("regression-") for g in doc["gates"])


def test_different_datasets_cannot_be_compared(run_model, gate, tmp_path: Path):
    _, _, base = run_model("qwen3.8-27b", name="base")
    run = json.loads(base.read_text(encoding="utf-8"))
    run["datasets"]["gov-qa-sw.jsonl"]["sha256"] = "0" * 64
    other = tmp_path / "other.json"
    other.write_text(json.dumps(run), encoding="utf-8")
    code, doc = gate(base, other)
    assert code == 1
    assert ("same-datasets", "all") in failed(doc)


def test_too_few_items_fails_coverage(run_model, gate):
    sw_only = ["--dataset", str(EVALS_DIR / "datasets" / "gov-qa-sw.jsonl")]
    _, _, path = run_model("qwen3.8-27b", extra=sw_only)
    code, doc = gate(path)
    assert code == 1
    assert {("coverage", "en"), ("coverage", "ar")} <= failed(doc)


def test_latency_gate(run_model, gate, tmp_path: Path):
    _, _, path = run_model("qwen3.8-27b")
    run = json.loads(path.read_text(encoding="utf-8"))
    run["summary"]["per_language"]["ar"]["latency_p95_s"] = 9.4
    slow = tmp_path / "slow.json"
    slow.write_text(json.dumps(run), encoding="utf-8")
    code, doc = gate(slow)
    assert code == 1
    assert failed(doc) == {("latency-p95", "ar")}


def test_judge_is_advisory_unless_criteria_set_it(run_model, gate, tmp_path: Path):
    _, run, path = run_model("qwen3.8-27b", extra=["--judge-model", "gpt-oss-120b"])
    assert run["summary"]["per_language"]["sw"]["judge_mean"] == 4.0
    code, _ = gate(path)
    assert code == 0
    criteria = yaml.safe_load((EVALS_DIR / "acceptance" / "criteria.yaml").read_text(encoding="utf-8"))
    criteria["judge"]["min_mean"] = 4.5
    strict = tmp_path / "strict.yaml"
    strict.write_text(yaml.safe_dump(criteria), encoding="utf-8")
    code, doc = gate(path, criteria=strict)
    assert code == 1
    assert ("judge-mean", "sw") in failed(doc)


def test_rag_target_reports_retrieval_recall(run_model):
    code, run, _ = run_model("qwen3.8-27b", extra=["--qa-target", "rag"])
    assert code == 0
    assert run["summary"]["per_language"]["sw"]["retrieval_recall"] == 1.0
    assert all(r["target"] == "rag" for r in run["results"] if r["kind"] != "redteam")

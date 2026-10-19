"""Shared fixtures: the mock platform wired into the harness through httpx.MockTransport.

Book: Sovereign AI Stacks, Chapter 11
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import harness
import pytest
import yaml
from evalkit.mock import EVALS_DIR, MockPlatform

ENV = {"EVAL_GATEWAY_KEY": "sk-test-eval-harness", "EVAL_RAG_TOKEN": "test-rag-token-123"}
URLS = ["--gateway-url", "http://gateway.test", "--rag-url", "http://rag.test"]


@pytest.fixture
def profiles() -> dict:
    return yaml.safe_load((EVALS_DIR / "mock" / "profiles.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def run_model(tmp_path: Path, profiles: dict):
    """Run the harness against the mock for one model; return (exit code, run dict)."""

    def _run(model: str, name: str | None = None, prof: dict | None = None, extra: list[str] | None = None):
        platform = MockPlatform.from_dir(profiles=copy.deepcopy(prof or profiles))
        out = tmp_path / (name or model)
        argv = ["run", "--model", model, *URLS, "--out", str(out), "--quiet", "--backoff", "0", *(extra or [])]
        code = harness.main(argv, env=dict(ENV), transport=platform.transport())
        run = json.loads((out / "run.json").read_text(encoding="utf-8")) if code == 0 else None
        return code, run, out / "run.json"

    return _run


@pytest.fixture
def gate(tmp_path: Path):
    """Run the gate; return (exit code, gate.json dict)."""

    def _gate(candidate: Path, baseline: Path | None = None, criteria: Path | None = None):
        out = tmp_path / "gate"
        argv = ["gate", "--candidate", str(candidate), "--out", str(out)]
        if baseline:
            argv += ["--baseline", str(baseline)]
        if criteria:
            argv += ["--criteria", str(criteria)]
        code = harness.main(argv, env={})
        return code, json.loads((out / "gate.json").read_text(encoding="utf-8"))

    return _gate


def failed(gate_doc: dict) -> set[tuple[str, str]]:
    return {(g["gate"], g["scope"]) for g in gate_doc["gates"] if not g["passed"]}

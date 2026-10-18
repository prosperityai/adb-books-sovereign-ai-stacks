"""Tests for report.py using result files in the format `vllm bench serve` 0.32 writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from report import load, main, render

FIX = Path(__file__).with_name("fixtures")


def test_loads_vllm_result_format() -> None:
    lv = load(FIX / "c16.json")
    assert lv.concurrency == 16 and lv.completed == 200
    assert lv.per_stream_tps == pytest.approx(1000 / 38.0)


def test_highest_passing_level_is_reported() -> None:
    levels = [load(f) for f in sorted(FIX.glob("c*.json"))]
    text, best = render(levels, slo_ttft=3000, slo_tpot=100)
    assert best is not None and best.concurrency == 32
    assert "| 64 |" in text and text.count("FAIL") == 1


def test_exit_code_when_nothing_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(f) for f in FIX.glob("c*.json")] + ["--slo-ttft-p95-ms", "1"]) == 1
    assert "No tested concurrency" in capsys.readouterr().out


def test_missing_percentiles_is_a_clear_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"max_concurrency": 1, "completed": 1}))
    with pytest.raises(SystemExit, match="metric-percentiles"):
        load(bad)

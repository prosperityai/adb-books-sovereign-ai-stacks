"""Tests for drill_report.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from drill_report import evaluate, human, load, main, parse_duration


def ev(ts: str, component: str, event: str, **extra: object) -> str:
    return json.dumps({"ts": ts, "component": component, "event": event, **extra})


GOOD = [
    ev("2026-10-19T09:00:00Z", "drill", "meta", detail="q4-2026"),
    ev("2026-10-19T09:00:00Z", "drill", "declared"),
    ev("2026-10-19T09:00:05Z", "litellm-db", "restore_started"),
    ev("2026-10-19T09:14:40Z", "litellm-db", "ready"),
    ev("2026-10-19T09:15:02Z", "litellm-db", "last_data", value="2026-10-19T08:58:50Z"),
    ev("2026-10-19T09:15:03Z", "litellm-db", "check", ok=True, detail="142 virtual keys"),
    ev("2026-10-19T09:15:04Z", "litellm-db", "verified"),
    ev("2026-10-19T09:00:06Z", "chat-ui", "restore_started"),
    ev("2026-10-19T09:31:10Z", "chat-ui", "ready"),
    ev("2026-10-19T09:31:30Z", "chat-ui", "last_data", value="2026-10-18T23:00:04Z"),
    ev("2026-10-19T09:31:40Z", "chat-ui", "check", ok=True, detail="1873 chats in webui.db"),
    ev("2026-10-19T09:31:41Z", "chat-ui", "verified"),
]


def test_durations_parse_and_print() -> None:
    assert parse_duration("1h30m") == 5400 and parse_duration("900") == 900 and parse_duration("45s") == 45
    assert human(3725) == "1h 02m 05s" and human(70) == "1m 10s" and human(9) == "9s" and human(None) == "n/a"


def test_rto_is_declaration_to_verification_and_rpo_is_data_gap() -> None:
    drill_id, declared, comps = load(GOOD)
    r = evaluate(drill_id, declared, comps, rto=3600, rpo=86400)
    db = next(c for c in r.components if c.name == "litellm-db")
    assert db.rto_seconds == 904 and db.rpo_seconds == 70 and db.restore_seconds == 875
    assert r.platform_rto_seconds == 1901  # chat-ui verified last
    assert r.platform_rpo_seconds == 36000 - 4  # nightly file backup: 10 hours old
    assert r.passed and r.drill_id == "q4-2026"


def test_rpo_target_breach_fails_the_drill() -> None:
    drill_id, declared, comps = load(GOOD)
    r = evaluate(drill_id, declared, comps, rto=3600, rpo=900)
    chat = next(c for c in r.components if c.name == "chat-ui")
    assert not r.passed and chat.status == "FAIL"
    assert chat.notes == ["RPO 9h 59m 56s exceeds target 15m 00s"]


def test_per_component_rpo_targets(tmp_path: Path) -> None:
    drill_id, declared, comps = load(GOOD)
    r = evaluate(drill_id, declared, comps, rto=3600, rpo=900, rpo_for={"chat-ui": 26 * 3600})
    assert r.passed and r.rpo_overrides == {"chat-ui": 93600}
    events = tmp_path / "e.jsonl"
    events.write_text("\n".join(GOOD), encoding="utf-8")
    assert main([str(events), "--rpo-for", "chat-ui=26h"]) == 0
    with pytest.raises(SystemExit):
        main([str(events), "--rpo-for", "chat-ui"])


def test_unverified_and_failed_checks_are_reported() -> None:
    lines = GOOD[:2] + [
        ev("2026-10-19T09:00:05Z", "keycloak-db", "restore_started"),
        ev("2026-10-19T09:20:00Z", "keycloak-db", "check", ok=False, detail="realm count 1, expected >= 2"),
        ev("2026-10-19T09:20:01Z", "keycloak-db", "failed", detail="timed out waiting for verification"),
    ]
    r = evaluate(*load(lines), rto=3600, rpo=900)
    kc = r.components[0]
    assert kc.status == "FAIL" and kc.rto_seconds is None and r.platform_rto_seconds is None
    assert "check failed: realm count 1, expected >= 2" in kc.notes and "never verified" in kc.notes


def test_negative_rpo_is_flagged() -> None:
    lines = GOOD[:2] + [
        ev("2026-10-19T09:00:05Z", "audit-db", "last_data", value="2026-10-19T09:02:00Z"),
        ev("2026-10-19T09:05:00Z", "audit-db", "verified"),
    ]
    r = evaluate(*load(lines), rto=3600, rpo=900)
    assert any("newer than the declaration" in n for n in r.components[0].notes)


def test_log_without_declaration_or_garbage_is_rejected() -> None:
    with pytest.raises(ValueError, match="declared"):
        load(GOOD[2:])
    with pytest.raises(ValueError, match="line 1"):
        load(["not json"])


def test_cli_writes_reports_and_sets_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(GOOD) + "\n", encoding="utf-8")
    md, js = tmp_path / "report.md", tmp_path / "report.json"
    assert main([str(events), "--rpo-target", "24h", "--markdown", str(md), "--json", str(js)]) == 0
    assert "| litellm-db | 14m 35s | 15m 04s | 1m 10s | 1/1 | PASS |" in md.read_text()
    assert json.loads(js.read_text())["passed"] is True
    assert main([str(events), "--rpo-target", "15m"]) == 1
    assert "Result: **FAIL**" in capsys.readouterr().out

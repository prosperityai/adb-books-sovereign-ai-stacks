"""Tests for obs_lint.py. Run with: pytest platform/observability/tools -q"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from obs_lint import (
    DEFAULT_DCGM_VALUES,
    DEFAULT_KNOWN,
    REPO_ROOT,
    Report,
    build_known,
    dcgm_fields_from_values,
    extract_rules,
    lint_dashboard,
    lint_rules,
    main,
    metric_names,
    parse_known_metrics,
    variable_selector,
)

OBS = REPO_ROOT / "platform" / "observability"
RULES = OBS / "alerts" / "llm-platform-rules.yaml"
DASHBOARDS = sorted((OBS / "dashboards").glob("*.json"))
KNOWN = build_known(DEFAULT_KNOWN, DEFAULT_DCGM_VALUES, [RULES])


# --- PromQL metric extraction ------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("up", {"up"}),
        ('rate(vllm:prompt_tokens_total{model_name="x"}[5m])', {"vllm:prompt_tokens_total"}),
        (
            "histogram_quantile(0.95, sum by (le, model_name) (rate(vllm:time_to_first_token_seconds_bucket[5m])))",
            {"vllm:time_to_first_token_seconds_bucket"},
        ),
        ("rate(DCGM_FI_DEV_THERMAL_VIOLATION[5m]) / 1e9 > 0.05", {"DCGM_FI_DEV_THERMAL_VIOLATION"}),
        ("a / on (pod) group_left (node) b", {"a", "b"}),
        ('count(DCGM_FI_DEV_GPU_TEMP{Hostname=~"$Hostname"} > 83) or vector(0)', {"DCGM_FI_DEV_GPU_TEMP"}),
        (
            'sum(increase(litellm_total_tokens_metric_total{team_alias=~"$team"}[$__range])) / 1e6 * $price_per_mtok',
            {"litellm_total_tokens_metric_total"},
        ),
        ("x > bool 1 unless y offset 1h", {"x", "y"}),
        ('absent(up{job="litellm-metrics"} == 1)', {"up"}),
    ],
)
def test_metric_names(expr: str, expected: set[str]) -> None:
    assert metric_names(expr) == expected


def test_label_values_trailing_label_is_not_a_metric() -> None:
    assert variable_selector("label_values(DCGM_FI_DEV_GPU_TEMP, Hostname)") == "DCGM_FI_DEV_GPU_TEMP"
    assert variable_selector('label_values(m{team_alias=~"$team"}, requested_model)') == 'm{team_alias=~"$team"}'
    assert variable_selector("label_values(namespace)") == ""
    assert variable_selector("query_result(topk(5, up))") == "topk(5, up)"


# --- known metrics -------------------------------------------------------------


def test_histograms_expand_to_bucket_sum_count() -> None:
    known = parse_known_metrics("foo_seconds histogram\nbar_total counter  # comment\n\n")
    assert known == {"foo_seconds_bucket", "foo_seconds_sum", "foo_seconds_count", "bar_total"}


def test_bad_known_metrics_line_is_rejected() -> None:
    with pytest.raises(ValueError, match="line 1"):
        parse_known_metrics("foo summary\n")


def test_dcgm_fields_come_from_the_exporter_values_csv() -> None:
    values = (
        "customMetrics: |\n"
        "  # comment\n"
        "  DCGM_FI_DEV_GPU_TEMP, gauge, help\n"
        "\n"
        "  DCGM_EXP_XID_ERRORS_TOTAL, counter, x\n"
    )
    assert dcgm_fields_from_values(values) == {"DCGM_FI_DEV_GPU_TEMP", "DCGM_EXP_XID_ERRORS_TOTAL"}


def test_platform_dcgm_values_enable_the_throttling_fields() -> None:
    fields = dcgm_fields_from_values(DEFAULT_DCGM_VALUES.read_text())
    assert {
        "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_SM_CLOCK",
        "DCGM_FI_DEV_CLOCKS_EVENT_REASONS",
        "DCGM_FI_DEV_THERMAL_VIOLATION",
        "DCGM_EXP_CLOCK_EVENTS_TOTAL",
        "DCGM_EXP_XID_ERRORS_TOTAL",
    } <= fields


# --- dashboards ----------------------------------------------------------------


def minimal_dashboard(expr: str = "up", ds: str = "prometheus") -> dict:
    return {
        "id": None,
        "uid": "test-dash",
        "title": "Test",
        "schemaVersion": 41,
        "panels": [
            {"id": 1, "type": "timeseries", "title": "p", "targets": [{"datasource": {"uid": ds}, "expr": expr}]}
        ],
    }


def test_clean_dashboard_passes() -> None:
    report = Report()
    lint_dashboard("d.json", minimal_dashboard(), KNOWN, report)
    assert report.ok, report.errors


def test_unknown_metric_is_reported() -> None:
    report = Report()
    lint_dashboard("d.json", minimal_dashboard("rate(vllm:prompt_tokens[5m])"), KNOWN, report)
    assert report.errors == ["d.json panel 1 ('p'): unknown metric 'vllm:prompt_tokens'"]


def test_dcgm_field_not_collected_is_reported() -> None:
    # A real DCGM field, but not in the exporter's counter list: a panel
    # using it would show "No data" forever.
    report = Report()
    lint_dashboard("d.json", minimal_dashboard("DCGM_FI_DEV_SYNC_BOOST_VIOLATION"), KNOWN, report)
    assert any("DCGM_FI_DEV_SYNC_BOOST_VIOLATION" in e for e in report.errors)


def test_unknown_datasource_is_reported() -> None:
    report = Report()
    lint_dashboard("d.json", minimal_dashboard(ds="grafanacloud-prom"), KNOWN, report)
    assert any("not provisioned" in e for e in report.errors)


def test_loki_targets_are_not_metric_checked() -> None:
    report = Report()
    lint_dashboard("d.json", minimal_dashboard('{namespace="ai-gateway"} |= "error"', ds="loki"), KNOWN, report)
    assert report.ok, report.errors


def test_links_outside_the_enclave_are_reported() -> None:
    dash = minimal_dashboard()
    dash["links"] = [{"url": "https://grafana.com/grafana/dashboards/12239"}]
    dash["panels"][0]["description"] = "See http://grafana.monitoring.svc:3000 or https://wiki.enclave.internal/gpu"
    report = Report()
    lint_dashboard("d.json", dash, KNOWN, report)
    assert report.errors == ["d.json: link to 'grafana.com' leaves the enclave"]


def test_duplicate_panel_ids_and_bad_uid_are_reported() -> None:
    dash = minimal_dashboard()
    dash["uid"] = "has spaces"
    dash["panels"].append(copy.deepcopy(dash["panels"][0]))
    report = Report()
    lint_dashboard("d.json", dash, KNOWN, report)
    assert any("uid" in e for e in report.errors)
    assert any("duplicate panel id" in e for e in report.errors)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_repository_dashboards_are_clean(path: Path) -> None:
    report = Report()
    lint_dashboard(path.name, json.loads(path.read_text()), KNOWN, report)
    assert report.ok, report.errors


def test_both_book_dashboards_exist() -> None:
    assert {p.name for p in DASHBOARDS} == {"gpu-fleet.json", "tokens-by-tenant.json"}


# --- rules ---------------------------------------------------------------------


def alert_doc(**overrides: object) -> dict:
    rule = {
        "alert": "Test",
        "expr": "up == 0",
        "labels": {"severity": "warning", "team": "platform"},
        "annotations": {
            "summary": "s",
            "description": "d",
            "runbook_url": "https://runbooks.enclave.internal/x",
        },
    }
    rule.update(overrides)
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {"name": "t", "namespace": "monitoring"},
        "spec": {"groups": [{"name": "g", "rules": [rule]}]},
    }


def test_clean_rule_passes() -> None:
    report = Report()
    lint_rules("r.yaml", [alert_doc()], KNOWN, report)
    assert report.ok, report.errors


def test_rule_without_severity_team_or_runbook_is_reported() -> None:
    report = Report()
    lint_rules(
        "r.yaml",
        [alert_doc(labels={"severity": "page"}, annotations={"summary": "s", "runbook_url": "https://example.com/rb"})],
        KNOWN,
        report,
    )
    joined = "\n".join(report.errors)
    assert "severity must be one of" in joined
    assert "team must be one of" in joined
    assert "missing annotation 'description'" in joined
    assert "runbook_url must start with" in joined


def test_recording_rule_naming_is_enforced() -> None:
    doc = alert_doc()
    doc["spec"]["groups"][0]["rules"] = [{"record": "ttft_p95", "expr": "up"}]
    report = Report()
    lint_rules("r.yaml", [doc], KNOWN, report)
    assert any("level:metric:operations" in e for e in report.errors)


def test_repository_rules_are_clean() -> None:
    report = Report()
    docs = list(yaml.safe_load_all(RULES.read_text()))
    lint_rules(RULES.name, docs, KNOWN, report)
    assert report.ok, report.errors


def test_rules_cover_the_chapter_requirements() -> None:
    docs = list(yaml.safe_load_all(RULES.read_text()))
    alerts = {r["alert"] for g in extract_rules(docs)["groups"] for r in g["rules"] if "alert" in r}
    assert {
        "LlmTtftSloBurnFast",
        "VllmQueueBacklog",
        "GpuThermalThrottling",
        "GpuFleetOverheating",
        "GpuXidCritical",
        "AuditSinkUnavailable",
    } <= alerts


def test_extract_rules_cli_writes_a_plain_rule_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-rules", str(RULES)]) == 0
    extracted = yaml.safe_load(capsys.readouterr().out)
    assert list(extracted) == ["groups"]
    assert extracted["groups"][0]["name"] == "llm-latency.rules"


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(minimal_dashboard()))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(minimal_dashboard("no_such_metric_total")))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert main(["dashboards", str(good)]) == 0
    assert main(["dashboards", str(bad)]) == 1
    assert main(["dashboards", str(broken)]) == 2
    out = capsys.readouterr().out
    assert "unknown metric 'no_such_metric_total'" in out

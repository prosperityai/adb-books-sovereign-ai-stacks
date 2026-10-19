#!/usr/bin/env python3
r"""obs_lint.py — lint Grafana dashboards and Prometheus alert rules offline.

Book: Sovereign AI Stacks, Chapter 9
Requires: Python 3.13, PyYAML 6.0

Three things break observability silently, and all three are checkable
before anything reaches the cluster:

1. A panel or alert queries a metric that does not exist (a renamed vLLM
   metric, a DCGM field the exporter was never configured to collect, a
   counter missing its ``_total`` suffix). Grafana shows "No data"; the alert
   never fires.
2. An alert without severity, team or runbook routes nowhere useful at 03:00.
3. A dashboard imported from the internet links back to the internet
   (grafana.com, external images, public docs) from inside the enclave.

Commands:
    obs_lint.py dashboards DASHBOARD.json [...]   lint dashboards
    obs_lint.py rules RULES.yaml [...]            lint PrometheusRule files
    obs_lint.py extract-rules RULES.yaml          print spec.groups as a plain
                                                  Prometheus rule file (promtool)

Known metric names come from known-metrics.txt next to this script, plus the
DCGM fields listed in platform/gpu/dcgm-exporter-values.yaml, plus every
recording rule defined in the rule files given with --rules.

Exit status: 0 clean, 1 lint errors, 2 usage or unreadable input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
DEFAULT_KNOWN = HERE / "known-metrics.txt"
DEFAULT_DCGM_VALUES = REPO_ROOT / "platform" / "gpu" / "dcgm-exporter-values.yaml"

HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
SEVERITIES = {"critical", "warning", "info"}
TEAMS = {"platform", "security"}
REQUIRED_ANNOTATIONS = ("summary", "description", "runbook_url")
RUNBOOK_PREFIX = "https://runbooks.enclave.internal/"
DATASOURCE_UIDS = {"prometheus", "loki", "${datasource}", "$datasource", "-- Grafana --", "grafana"}
# Hosts a dashboard may link to. Anything else is outside the enclave.
ALLOWED_URL_HOST = re.compile(r"^([a-z0-9-]+\.)*(enclave\.internal|svc|svc\.cluster\.local)(:\d+)?$")

PROMQL_KEYWORDS = {
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "bool",
    "offset",
    "and",
    "or",
    "unless",
    "atan2",
    "inf",
    "nan",
}

_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`')
_BRACES = re.compile(r"\{[^{}]*\}")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
_IDENT = re.compile(r"(?<![\w:.$])([A-Za-z_:][A-Za-z0-9_:]*)(?![\w:])")
_URL = re.compile(r"https?://([^/\s\"'?#]+)")


@dataclass
class Report:
    """Collects findings; one line per problem, prefixed with where it is."""

    errors: list[str] = field(default_factory=list)

    def add(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    @property
    def ok(self) -> bool:
        return not self.errors


# --- known metrics ---------------------------------------------------------


def parse_known_metrics(text: str) -> set[str]:
    """Parse ``<name> <type>`` lines; histograms expand to their three series."""
    names: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[1] not in {"counter", "gauge", "histogram"}:
            raise ValueError(f"known-metrics line {lineno}: expected '<name> <counter|gauge|histogram>'")
        name, kind = parts
        if kind == "histogram":
            names.update(name + suffix for suffix in HISTOGRAM_SUFFIXES)
        else:
            names.add(name)
    return names


def dcgm_fields_from_values(values_text: str) -> set[str]:
    """Return the DCGM field names the exporter is configured to collect.

    Reads the chart's ``customMetrics`` CSV (field, type, help). Lines that
    start with '#' are comments, exactly as the exporter treats them.
    """
    values = yaml.safe_load(values_text) or {}
    csv = values.get("customMetrics") or ""
    fields: set[str] = set()
    for raw in csv.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(",", 1)[0].strip()
        if name:
            fields.add(name)
    return fields


def recording_rules(documents: Iterable[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for doc in documents:
        for group in rule_groups(doc):
            for rule in group.get("rules") or []:
                if "record" in rule:
                    names.add(rule["record"])
    return names


# --- PromQL ------------------------------------------------------------------


def metric_names(expr: str) -> set[str]:
    """Return the metric names a PromQL expression selects.

    Not a full parser: it removes strings, label matchers, range selectors and
    grouping clauses, then keeps identifiers that are not followed by '(' (a
    function or aggregation) and are not keywords. Grafana variables ($x,
    ${x}, $__rate_interval) are ignored.
    """
    text = _STRING.sub(" ", expr)
    text = _BRACES.sub(" ", text)
    text = _BRACKETS.sub(" ", text)
    text = _GROUPING.sub(" ", text)
    names: set[str] = set()
    for match in _IDENT.finditer(text):
        name = match.group(1)
        if name.lower() in PROMQL_KEYWORDS:
            continue
        rest = text[match.end() :].lstrip()
        if rest.startswith("("):
            continue
        names.add(name)
    return names


def unknown_metrics(expr: str, known: set[str]) -> set[str]:
    return {name for name in metric_names(expr) if name not in known}


# --- dashboards --------------------------------------------------------------


def iter_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield every panel, including those nested inside collapsed rows."""
    for panel in panels:
        yield panel
        yield from iter_panels(panel.get("panels") or [])


def iter_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_strings(value)


def datasource_uid(node: dict[str, Any]) -> str | None:
    ds = node.get("datasource")
    if isinstance(ds, dict):
        return ds.get("uid")
    if isinstance(ds, str):
        return ds
    return None


_LABEL_VALUES = re.compile(r"^\s*label_values\s*\((?P<body>.*)\)\s*$", re.DOTALL)
_QUERY_RESULT = re.compile(r"^\s*query_result\s*\((?P<body>.*)\)\s*$", re.DOTALL)


def variable_selector(query: str) -> str:
    """Return the PromQL part of a Grafana variable query.

    ``label_values(metric{...}, label)`` selects ``metric``; the trailing
    label name is not a metric. ``label_values(label)`` selects nothing.
    ``query_result(expr)`` is ``expr``.
    """
    if match := _LABEL_VALUES.match(query):
        body = match.group("body")
        selector, sep, _label = body.rpartition(",")
        return selector if sep else ""
    if match := _QUERY_RESULT.match(query):
        return match.group("body")
    return query


def lint_dashboard(name: str, dashboard: dict[str, Any], known: set[str], report: Report) -> None:
    for key in ("uid", "title", "schemaVersion", "panels"):
        if key not in dashboard:
            report.add(name, f"missing top-level key '{key}'")
    uid = dashboard.get("uid", "")
    if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", uid):
        report.add(name, f"uid {uid!r} must be 1-40 characters of [A-Za-z0-9_-]")
    if dashboard.get("id") is not None:
        report.add(name, "'id' must be null so Grafana assigns it on provisioning")

    seen_ids: set[int] = set()
    for panel in iter_panels(dashboard.get("panels") or []):
        pid = panel.get("id")
        where = f"{name} panel {pid} ({panel.get('title', '?')!r})"
        if pid in seen_ids:
            report.add(where, "duplicate panel id")
        seen_ids.add(pid)
        if panel.get("type") == "row":
            continue
        panel_ds = datasource_uid(panel)
        for target in panel.get("targets") or []:
            ds = datasource_uid(target) or panel_ds
            if ds not in DATASOURCE_UIDS:
                report.add(where, f"datasource uid {ds!r} is not provisioned on this platform")
            expr = target.get("expr")
            if not expr:
                continue
            if ds == "loki":
                continue  # LogQL: label names are not metric names
            for metric in sorted(unknown_metrics(expr, known)):
                report.add(where, f"unknown metric '{metric}'")

    for variable in (dashboard.get("templating") or {}).get("list") or []:
        query = variable.get("query")
        if isinstance(query, dict):
            query = query.get("query")
        if variable.get("type") == "query" and isinstance(query, str):
            inner = variable_selector(query)
            for metric in sorted(unknown_metrics(inner, known)):
                report.add(f"{name} variable {variable.get('name')!r}", f"unknown metric '{metric}'")

    for text in iter_strings(dashboard):
        for host in _URL.findall(text):
            if not ALLOWED_URL_HOST.match(host.lower()):
                report.add(name, f"link to '{host}' leaves the enclave")


# --- rules -------------------------------------------------------------------


def rule_groups(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Groups from a PrometheusRule (spec.groups) or a plain rule file (groups)."""
    if not isinstance(doc, dict):
        return []
    if doc.get("kind") == "PrometheusRule":
        return (doc.get("spec") or {}).get("groups") or []
    return doc.get("groups") or []


def lint_rules(name: str, documents: list[dict[str, Any]], known: set[str], report: Report) -> None:
    alert_names: set[str] = set()
    for doc in documents:
        if doc.get("kind") == "PrometheusRule" and not (doc.get("metadata") or {}).get("namespace"):
            report.add(name, "PrometheusRule needs metadata.namespace")
        for group in rule_groups(doc):
            gname = group.get("name", "?")
            for rule in group.get("rules") or []:
                label = rule.get("alert") or rule.get("record") or "?"
                where = f"{name} group {gname!r} rule {label!r}"
                expr = str(rule.get("expr", ""))
                if not expr.strip():
                    report.add(where, "empty expr")
                for metric in sorted(unknown_metrics(expr, known)):
                    report.add(where, f"unknown metric '{metric}'")
                if "record" in rule:
                    if rule["record"].count(":") < 2:
                        report.add(where, "recording rule names follow level:metric:operations")
                    continue
                if label in alert_names:
                    report.add(where, "duplicate alert name")
                alert_names.add(label)
                labels = rule.get("labels") or {}
                if labels.get("severity") not in SEVERITIES:
                    report.add(where, f"severity must be one of {sorted(SEVERITIES)}")
                if labels.get("team") not in TEAMS:
                    report.add(where, f"team must be one of {sorted(TEAMS)}")
                annotations = rule.get("annotations") or {}
                for key in REQUIRED_ANNOTATIONS:
                    if not annotations.get(key):
                        report.add(where, f"missing annotation '{key}'")
                runbook = annotations.get("runbook_url", "")
                if runbook and not runbook.startswith(RUNBOOK_PREFIX):
                    report.add(where, f"runbook_url must start with {RUNBOOK_PREFIX}")


def extract_rules(documents: list[dict[str, Any]]) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    for doc in documents:
        groups.extend(rule_groups(doc))
    return {"groups": groups}


# --- CLI ---------------------------------------------------------------------


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def build_known(known_file: Path, dcgm_values: Path, rule_files: list[Path]) -> set[str]:
    known = parse_known_metrics(known_file.read_text(encoding="utf-8"))
    if dcgm_values.exists():
        known |= dcgm_fields_from_values(dcgm_values.read_text(encoding="utf-8"))
    for path in rule_files:
        known |= recording_rules(load_yaml_documents(path))
    return known


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--known", type=Path, default=DEFAULT_KNOWN, help="known-metrics file")
    parser.add_argument("--dcgm-values", type=Path, default=DEFAULT_DCGM_VALUES, help="DCGM exporter values file")
    parser.add_argument(
        "--rules", type=Path, action="append", default=[], help="rule file whose recording rules count as known"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("dashboards", "rules", "extract-rules"):
        p = sub.add_parser(cmd)
        p.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)

    try:
        if args.command == "extract-rules":
            docs: list[dict[str, Any]] = []
            for path in args.files:
                docs.extend(load_yaml_documents(path))
            sys.stdout.write(yaml.safe_dump(extract_rules(docs), sort_keys=False, width=120))
            return 0

        rule_files = list(args.rules)
        if args.command == "rules":
            rule_files += args.files
        known = build_known(args.known, args.dcgm_values, rule_files)
        report = Report()
        for path in args.files:
            if args.command == "dashboards":
                lint_dashboard(path.name, json.loads(path.read_text(encoding="utf-8")), known, report)
            else:
                lint_rules(path.name, load_yaml_documents(path), known, report)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"obs_lint: {exc}", file=sys.stderr)
        return 2

    for line in report.errors:
        print(line)
    checked = ", ".join(p.name for p in args.files)
    if report.ok:
        print(f"OK: {checked} ({len(known)} known metric names)")
        return 0
    print(f"FAILED: {len(report.errors)} problem(s) in {checked}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

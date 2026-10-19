#!/usr/bin/env python3
"""drill_report.py — turn a DR drill's event log into measured RTO and RPO.

Book: Sovereign AI Stacks, Chapter 12
Requires: Python 3.13 (standard library only)

    python3 drill_report.py drills/20261019T0900Z/events.jsonl --rto-target 60m --rpo-target 15m \
        --rpo-for chat-ui=26h --rpo-for qdrant=26h \
        --markdown drills/20261019T0900Z/report.md --json drills/20261019T0900Z/report.json

dr_drill.sh appends one JSON object per line as the drill runs:

    {"ts": "2026-10-19T09:00:00Z", "component": "drill", "event": "declared"}
    {"ts": "...", "component": "litellm-db", "event": "restore_started"}
    {"ts": "...", "component": "litellm-db", "event": "ready"}
    {"ts": "...", "component": "litellm-db", "event": "last_data", "value": "2026-10-19T08:59:12Z"}
    {"ts": "...", "component": "litellm-db", "event": "check", "ok": true, "detail": "142 virtual keys"}
    {"ts": "...", "component": "litellm-db", "event": "verified"}
    {"ts": "...", "component": "chat-ui", "event": "failed", "detail": "restore PartiallyFailed"}

RTO is measured from the moment the disaster is declared to the moment a
component is VERIFIED (not merely running). RPO is the gap between the
declaration and the newest data the restored copy actually contains.
Exit code: 0 if every component recovered within both targets, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(text: str) -> int:
    """'1h30m', '45m', '90s' or a bare number of seconds -> seconds."""
    if text.isdigit():
        return int(text)
    m = DURATION.match(text)
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"bad duration {text!r}; use e.g. 60m, 1h30m, 900")
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def parse_ts(text: str) -> datetime:
    ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return ts.astimezone(UTC)


def human(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(round(seconds))
    sign = "-" if s < 0 else ""
    s = abs(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{sign}{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{sign}{m}m {sec:02d}s"
    return f"{sign}{sec}s"


@dataclass
class Component:
    name: str
    restore_started: datetime | None = None
    ready: datetime | None = None
    verified: datetime | None = None
    last_data: datetime | None = None
    checks: list[tuple[bool, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    rto_seconds: float | None
    rpo_seconds: float | None
    restore_seconds: float | None
    checks_passed: int
    checks_total: int
    notes: list[str]


@dataclass(frozen=True)
class Report:
    drill_id: str
    declared_at: str
    rto_target_seconds: int
    rpo_target_seconds: int
    rpo_overrides: dict[str, int]
    platform_rto_seconds: float | None
    platform_rpo_seconds: float | None
    passed: bool
    components: list[Result]


def load(lines: list[str]) -> tuple[str, datetime, dict[str, Component]]:
    declared: datetime | None = None
    drill_id = "unnamed"
    comps: dict[str, Component] = {}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            ts = parse_ts(ev["ts"])
            name, kind = ev["component"], ev["event"]
        except (ValueError, KeyError) as exc:
            raise ValueError(f"line {n}: not a drill event: {exc}") from exc
        if name == "drill":
            if kind == "declared":
                declared = ts
            elif kind == "meta":
                drill_id = ev.get("detail", drill_id)
            continue
        c = comps.setdefault(name, Component(name))
        if kind in ("restore_started", "ready", "verified"):
            setattr(c, kind, ts)
        elif kind == "last_data":
            c.last_data = parse_ts(ev["value"])
        elif kind == "check":
            c.checks.append((bool(ev.get("ok")), ev.get("detail", "")))
        elif kind == "failed":
            c.failures.append(ev.get("detail", "failed"))
        else:
            raise ValueError(f"line {n}: unknown event {kind!r}")
    if declared is None:
        raise ValueError("no 'declared' event: the drill never declared the disaster")
    return drill_id, declared, comps


def evaluate(
    drill_id: str,
    declared: datetime,
    comps: dict[str, Component],
    rto: int,
    rpo: int,
    rpo_for: dict[str, int] | None = None,
) -> Report:
    """`rpo_for` overrides the RPO target per component: a nightly file backup
    and a WAL-archived database do not promise the same data loss."""
    rpo_for = rpo_for or {}
    results = []
    for c in sorted(comps.values(), key=lambda x: x.name):
        target_rpo = rpo_for.get(c.name, rpo)
        notes = list(c.failures)
        rto_s = (c.verified - declared).total_seconds() if c.verified else None
        rpo_s = (declared - c.last_data).total_seconds() if c.last_data else None
        restore_s = (c.ready - c.restore_started).total_seconds() if c.ready and c.restore_started else None
        notes += [f"check failed: {d}" for ok, d in c.checks if not ok]
        if c.verified is None:
            notes.append("never verified")
        if rpo_s is None:
            notes.append("no data timestamp recovered: RPO unknown")
        elif rpo_s < 0:
            notes.append("restored data is newer than the declaration: clock skew or the source kept writing")
        if rto_s is not None and rto_s > rto:
            notes.append(f"RTO {human(rto_s)} exceeds target {human(rto)}")
        if rpo_s is not None and rpo_s > target_rpo:
            notes.append(f"RPO {human(rpo_s)} exceeds target {human(target_rpo)}")
        passed = sum(1 for ok, _ in c.checks if ok)
        results.append(
            Result(c.name, "FAIL" if notes else "PASS", rto_s, rpo_s, restore_s, passed, len(c.checks), notes)
        )
    rtos = [r.rto_seconds for r in results]
    rpos = [r.rpo_seconds for r in results]
    platform_rto = None if None in rtos or not rtos else max(rtos)  # type: ignore[type-var]
    platform_rpo = None if None in rpos or not rpos else max(rpos)  # type: ignore[type-var]
    ok = bool(results) and all(r.status == "PASS" for r in results)
    return Report(
        drill_id, declared.strftime("%Y-%m-%dT%H:%M:%SZ"), rto, rpo, rpo_for, platform_rto, platform_rpo, ok, results
    )


def render_markdown(r: Report) -> str:
    out = [
        f"# DR drill report: {r.drill_id}",
        "",
        f"- Disaster declared: {r.declared_at}",
        f"- Targets: RTO {human(r.rto_target_seconds)}, RPO {human(r.rpo_target_seconds)}"
        + "".join(f"; {name} RPO {human(v)}" for name, v in sorted(r.rpo_overrides.items())),
        f"- **Measured platform RTO: {human(r.platform_rto_seconds)}**",
        f"- **Measured platform RPO: {human(r.platform_rpo_seconds)}**",
        f"- Result: **{'PASS' if r.passed else 'FAIL'}**",
        "",
        "| Component | Restore | RTO | RPO | Checks | Status |",
        "|---|---|---|---|---|---|",
    ]
    for c in r.components:
        out.append(
            f"| {c.name} | {human(c.restore_seconds)} | {human(c.rto_seconds)} | {human(c.rpo_seconds)} "
            f"| {c.checks_passed}/{c.checks_total} | {c.status} |"
        )
    notes = [(c.name, n) for c in r.components for n in c.notes]
    if notes:
        out += ["", "## Findings", ""] + [f"- **{name}**: {n}" for name, n in notes]
    out += [
        "",
        "RTO runs from declaration to verification. RPO is the declaration time minus the newest",
        "record found in the restored copy. Heartbeat resolution is 10 s.",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compute RTO/RPO from a DR drill event log.")
    p.add_argument("events", type=Path)
    p.add_argument("--rto-target", type=parse_duration, default=parse_duration("60m"))
    p.add_argument("--rpo-target", type=parse_duration, default=parse_duration("15m"))
    p.add_argument(
        "--rpo-for", action="append", default=[], metavar="COMPONENT=DURATION",
        help="per-component RPO target, e.g. chat-ui=26h (repeatable)",
    )
    p.add_argument("--markdown", type=Path)
    p.add_argument("--json", type=Path)
    a = p.parse_args(argv)
    drill_id, declared, comps = load(a.events.read_text(encoding="utf-8").splitlines())
    rpo_for = {}
    for item in a.rpo_for:
        name, _, value = item.partition("=")
        if not name or not value:
            p.error(f"--rpo-for expects COMPONENT=DURATION, got {item!r}")
        rpo_for[name] = parse_duration(value)
    report = evaluate(drill_id, declared, comps, a.rto_target, a.rpo_target, rpo_for)
    md = render_markdown(report)
    if a.markdown:
        a.markdown.write_text(md, encoding="utf-8")
    if a.json:
        a.json.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    print(md)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

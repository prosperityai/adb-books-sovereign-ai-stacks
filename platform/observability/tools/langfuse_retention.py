#!/usr/bin/env python3
r"""langfuse_retention.py — delete Langfuse traces older than N days.

Book: Sovereign AI Stacks, Chapter 9
Requires: Python 3.13, standard library only (runs in python:3.13.16-slim)

Langfuse's own data-retention setting is an Enterprise Edition feature on
self-hosted instances; the open-source build keeps every trace forever. Each
trace holds a full prompt and response, so "forever" contradicts the retention
policy the platform promises the regulator (Chapter 10).

This script enforces the window through the public API, which the open-source
build does serve:

1. GET  /api/public/v2/observations?isRootObservation=true&toStartTime=<cutoff>
   (cursor-paginated, up to 1,000 per page) to find traces that started
   before the cutoff.
2. DELETE /api/public/traces with {"traceIds": [...]} in batches.

Authentication is HTTP Basic with the project's public key as the user and
its secret key as the password, read from LANGFUSE_PUBLIC_KEY and
LANGFUSE_SECRET_KEY. Deletion is queued by Langfuse and completes
asynchronously; a trace deleted twice is harmless.

Example:
    LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
      python3 langfuse_retention.py --host http://langfuse-web.monitoring.svc:3000 --days 30 --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

PAGE_LIMIT = 1000  # API maximum for /api/public/v2/observations
TIMEOUT_SECONDS = 60


class LangfuseError(RuntimeError):
    """An API call failed; the message says which call and why."""


@dataclass(frozen=True)
class Client:
    host: str
    public_key: str
    secret_key: str

    def _request(self, method: str, path: str, query: dict[str, str] | None = None, body: object = None) -> object:
        if not self.host.startswith(("http://", "https://")):
            raise LangfuseError(f"host must start with http:// or https://, got {self.host!r}")
        url = self.host.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - scheme checked above
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise LangfuseError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LangfuseError(f"{method} {path} -> {exc.reason}") from exc
        return json.loads(payload) if payload else {}

    def old_trace_ids(self, cutoff: datetime) -> Iterator[str]:
        """Yield each trace ID whose root observation started before cutoff, once."""
        seen: set[str] = set()
        cursor: str | None = None
        while True:
            query = {
                "isRootObservation": "true",
                "toStartTime": cutoff.isoformat().replace("+00:00", "Z"),
                "fields": "core",
                "limit": str(PAGE_LIMIT),
            }
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", "/api/public/v2/observations", query=query)
            if not isinstance(page, dict):
                raise LangfuseError("GET /api/public/v2/observations returned a non-object body")
            for observation in page.get("data") or []:
                trace_id = observation.get("traceId")
                if trace_id and trace_id not in seen:
                    seen.add(trace_id)
                    yield trace_id
            cursor = (page.get("meta") or {}).get("cursor")
            if not cursor:
                return

    def delete_traces(self, trace_ids: list[str]) -> None:
        self._request("DELETE", "/api/public/traces", body={"traceIds": trace_ids})


def batched(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def run(
    client: Client, days: int, batch_size: int, max_traces: int, dry_run: bool, now: datetime | None = None
) -> dict[str, object]:
    if days < 3:
        raise ValueError("--days must be at least 3; shorter windows delete traces still under investigation")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    trace_ids: list[str] = []
    for trace_id in client.old_trace_ids(cutoff):
        trace_ids.append(trace_id)
        if len(trace_ids) >= max_traces:
            break
    batches = 0
    if not dry_run:
        for batch in batched(trace_ids, batch_size):
            client.delete_traces(batch)
            batches += 1
    return {
        "cutoff": cutoff.isoformat(),
        "traces_found": len(trace_ids),
        "traces_deleted": 0 if dry_run else len(trace_ids),
        "delete_calls": batches,
        "dry_run": dry_run,
        "more_remaining": len(trace_ids) >= max_traces,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="Langfuse base URL, e.g. http://langfuse-web.monitoring.svc:3000")
    parser.add_argument("--days", type=int, default=30, help="keep traces newer than this many days (min 3)")
    parser.add_argument("--batch-size", type=int, default=100, help="trace IDs per DELETE call")
    parser.add_argument("--max-traces", type=int, default=50_000, help="stop after this many per run")
    parser.add_argument("--dry-run", action="store_true", help="count, do not delete")
    args = parser.parse_args(argv)

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not public_key or not secret_key:
        print("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set", file=sys.stderr)
        return 2
    if args.batch_size < 1 or args.max_traces < 1:
        print("--batch-size and --max-traces must be positive", file=sys.stderr)
        return 2

    client = Client(host=args.host, public_key=public_key, secret_key=secret_key)
    try:
        summary = run(client, args.days, args.batch_size, args.max_traces, args.dry_run)
    except (LangfuseError, ValueError) as exc:
        print(f"langfuse_retention: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

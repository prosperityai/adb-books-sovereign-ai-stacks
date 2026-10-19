"""Tests for langfuse_retention.py against an in-process fake Langfuse API.

Run with: pytest platform/observability/tools -q
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langfuse_retention import Client, LangfuseError, batched, main, run

PUBLIC, SECRET = "pk-lf-test", "sk-lf-test"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class FakeLangfuse:
    """Serves GET /api/public/v2/observations and DELETE /api/public/traces."""

    def __init__(self, observations: list[dict], page_size: int = 2) -> None:
        self.observations = observations
        self.page_size = page_size
        self.get_queries: list[dict[str, str]] = []
        self.deleted_batches: list[list[str]] = []
        self.fail_delete = False


@pytest.fixture
def fake() -> Iterator[tuple[FakeLangfuse, str]]:
    state = FakeLangfuse([])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _authorised(self) -> bool:
            expected = "Basic " + base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
            if self.headers.get("Authorization") != expected:
                self._send(401, {"message": "Invalid credentials"})
                return False
            return True

        def _send(self, code: int, body: object) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if not self._authorised():
                return
            url = urllib.parse.urlparse(self.path)
            if url.path != "/api/public/v2/observations":
                self._send(404, {})
                return
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            state.get_queries.append(query)
            cutoff = datetime.fromisoformat(query["toStartTime"].replace("Z", "+00:00"))
            matching = [
                o
                for o in state.observations
                if datetime.fromisoformat(o["startTime"]) < cutoff
                and (query.get("isRootObservation") != "true" or o["isRootObservation"])
            ]
            start = int(query.get("cursor", "0"))
            page = matching[start : start + state.page_size]
            meta = {"cursor": str(start + state.page_size)} if start + state.page_size < len(matching) else {}
            self._send(200, {"data": page, "meta": meta})

        def do_DELETE(self) -> None:  # noqa: N802 - http.server API
            if not self._authorised():
                return
            if self.path != "/api/public/traces":
                self._send(404, {})
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if state.fail_delete:
                self._send(500, {"message": "clickhouse unavailable"})
                return
            state.deleted_batches.append(body["traceIds"])
            self._send(200, {"message": "Traces deleted"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def seed(state: FakeLangfuse) -> None:
    # With --days 30 and NOW = 19 Oct 2026 12:00 UTC, the cutoff is 19 Sep 12:00.
    state.observations = [
        {"id": "a", "traceId": "t-old-1", "startTime": "2026-08-01T10:00:00+00:00", "isRootObservation": True},
        {"id": "a2", "traceId": "t-old-1", "startTime": "2026-08-01T10:00:01+00:00", "isRootObservation": False},
        {"id": "b", "traceId": "t-old-2", "startTime": "2026-09-01T10:00:00+00:00", "isRootObservation": True},
        {"id": "c", "traceId": "t-old-3", "startTime": "2026-09-18T23:59:00+00:00", "isRootObservation": True},
        {"id": "d", "traceId": "t-new-1", "startTime": "2026-10-01T10:00:00+00:00", "isRootObservation": True},
        {"id": "e", "traceId": "t-new-2", "startTime": "2026-10-19T11:00:00+00:00", "isRootObservation": True},
    ]


def test_deletes_only_traces_older_than_the_window(fake: tuple[FakeLangfuse, str]) -> None:
    state, url = fake
    seed(state)
    summary = run(Client(url, PUBLIC, SECRET), days=30, batch_size=100, max_traces=1000, dry_run=False, now=NOW)
    assert state.deleted_batches == [["t-old-1", "t-old-2", "t-old-3"]]
    assert summary["traces_deleted"] == 3
    assert summary["cutoff"] == "2026-09-19T12:00:00+00:00"
    assert state.get_queries[0]["toStartTime"] == "2026-09-19T12:00:00Z"
    assert state.get_queries[0]["isRootObservation"] == "true"
    assert state.get_queries[0]["limit"] == "1000"


def test_follows_the_cursor_across_pages(fake: tuple[FakeLangfuse, str]) -> None:
    state, url = fake
    seed(state)
    state.page_size = 1
    run(Client(url, PUBLIC, SECRET), days=30, batch_size=100, max_traces=1000, dry_run=False, now=NOW)
    assert len(state.get_queries) == 3
    assert [q.get("cursor") for q in state.get_queries] == [None, "1", "2"]


def test_batches_delete_calls(fake: tuple[FakeLangfuse, str]) -> None:
    state, url = fake
    seed(state)
    summary = run(Client(url, PUBLIC, SECRET), days=30, batch_size=2, max_traces=1000, dry_run=False, now=NOW)
    assert state.deleted_batches == [["t-old-1", "t-old-2"], ["t-old-3"]]
    assert summary["delete_calls"] == 2


def test_dry_run_deletes_nothing(fake: tuple[FakeLangfuse, str]) -> None:
    state, url = fake
    seed(state)
    summary = run(Client(url, PUBLIC, SECRET), days=30, batch_size=100, max_traces=1000, dry_run=True, now=NOW)
    assert state.deleted_batches == []
    assert summary["traces_found"] == 3
    assert summary["traces_deleted"] == 0


def test_max_traces_caps_a_run_and_reports_more_remaining(fake: tuple[FakeLangfuse, str]) -> None:
    state, url = fake
    seed(state)
    summary = run(Client(url, PUBLIC, SECRET), days=30, batch_size=100, max_traces=2, dry_run=False, now=NOW)
    assert state.deleted_batches == [["t-old-1", "t-old-2"]]
    assert summary["more_remaining"] is True


def test_windows_under_three_days_are_refused(fake: tuple[FakeLangfuse, str]) -> None:
    _, url = fake
    with pytest.raises(ValueError, match="at least 3"):
        run(Client(url, PUBLIC, SECRET), days=1, batch_size=100, max_traces=10, dry_run=True, now=NOW)


def test_wrong_credentials_raise_a_clear_error(fake: tuple[FakeLangfuse, str]) -> None:
    _, url = fake
    with pytest.raises(LangfuseError, match="HTTP 401"):
        list(Client(url, PUBLIC, "wrong").old_trace_ids(NOW))


def test_host_without_scheme_is_refused() -> None:
    # LiteLLM silently prefixes https:// to a bare host; this script refuses instead.
    with pytest.raises(LangfuseError, match="http:// or https://"):
        list(Client("langfuse-web.monitoring.svc:3000", PUBLIC, SECRET).old_trace_ids(NOW))


def test_server_error_on_delete_fails_the_run(
    fake: tuple[FakeLangfuse, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = fake
    seed(state)
    state.fail_delete = True
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET)
    assert main(["--host", url, "--days", "30"]) == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_cli_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert main(["--host", "http://127.0.0.1:1"]) == 2


def test_cli_dry_run_prints_json_summary(
    fake: tuple[FakeLangfuse, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = fake
    seed(state)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET)
    assert main(["--host", url, "--days", "3650", "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "cutoff": summary["cutoff"],
        "traces_found": 0,
        "traces_deleted": 0,
        "delete_calls": 0,
        "dry_run": True,
        "more_remaining": False,
    }


def test_batched() -> None:
    assert list(batched(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]
    assert list(batched([], 5)) == []

"""The audit sink: receives LiteLLM's generic_api events, redacts, stores.

Book: Sovereign AI Stacks, Chapter 10 (threats T8, T9)

    POST /v1/events   LiteLLM StandardLoggingPayload batch (bearer token)
    GET  /healthz     process is alive
    GET  /readyz      database reachable
    GET  /metrics     Prometheus counters (no content, no identities)

Run: uvicorn audit.sink:create_app --factory --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from audit.models import AuditRecord, PayloadError, RetentionPolicy, parse_body, to_record
from audit.redact import Redactor
from audit.store import PostgresRepository, Repository, StoreUnavailable

log = logging.getLogger("audit.sink")
MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class Settings:
    token: str
    retention: RetentionPolicy
    trusted_user_keys: frozenset[str]
    db_conninfo: str = ""
    max_body_bytes: int = 64 * 1024 * 1024

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        token = e.get("AUDIT_SINK_TOKEN", "")
        if not token and e.get("AUDIT_SINK_TOKEN_FILE"):
            with open(e["AUDIT_SINK_TOKEN_FILE"], encoding="utf-8") as f:
                token = f.read().strip()
        conninfo = " ".join(
            f"{k}={v}"
            for k, v in (
                ("host", e.get("AUDIT_DB_HOST", "audit-db-rw.ai-gateway.svc")),
                ("port", e.get("AUDIT_DB_PORT", "5432")),
                ("dbname", e.get("AUDIT_DB_NAME", "audit")),
                ("user", e.get("AUDIT_DB_USER", "audit_writer")),
                ("password", e.get("AUDIT_DB_PASSWORD", "")),
                ("sslmode", e.get("AUDIT_DB_SSLMODE", "verify-full")),
                ("sslrootcert", e.get("AUDIT_DB_SSLROOTCERT", "/etc/audit-db-ca/ca.crt")),
            )
            if v
        )
        return Settings(
            token=token,
            retention=RetentionPolicy.from_json(e.get("AUDIT_RETENTION_DAYS", '{"*": 365}')),
            trusted_user_keys=frozenset(
                k.strip() for k in e.get("AUDIT_TRUSTED_USER_KEYS", "").split(",") if k.strip()
            ),
            db_conninfo=conninfo,
            max_body_bytes=int(e.get("AUDIT_MAX_BODY_BYTES", str(64 * 1024 * 1024))),
        )


class Metrics:
    """A handful of counters in Prometheus text format; no client library needed."""

    def __init__(self) -> None:
        self._c: Counter[tuple[str, str]] = Counter()
        self._lock = threading.Lock()

    def inc(self, name: str, label: str = "", n: int = 1) -> None:
        with self._lock:
            self._c[(name, label)] += n

    def render(self) -> str:
        lines = []
        with self._lock:
            for (name, label), v in sorted(self._c.items()):
                lbl = f"{{{label}}}" if label else ""
                lines.append(f"audit_sink_{name}{lbl} {v}")
        return "\n".join(lines) + "\n"


def _authorised(header: str | None, token: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), token.encode())


def create_app(
    settings: Settings | None = None,
    repository: Repository | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    # Fail closed: an audit endpoint without authentication would accept
    # forged records from anything that can reach it. Refuse to start.
    if len(settings.token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"AUDIT_SINK_TOKEN missing or shorter than {MIN_TOKEN_LENGTH} characters: refusing to start")
    owned_pg = repository is None
    repo: Repository = repository or PostgresRepository(settings.db_conninfo)
    redactor = Redactor()
    metrics = Metrics()
    now = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owned_pg and isinstance(repo, PostgresRepository):
            repo.open()
        yield
        if owned_pg and isinstance(repo, PostgresRepository):
            repo.close()

    app = FastAPI(title="audit-sink", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def process(body: bytes) -> tuple[int, dict]:
        try:
            events = parse_body(body)
        except (PayloadError, UnicodeDecodeError) as err:
            return 400, {"error": f"invalid payload: {err}"}
        records: list[AuditRecord] = []
        rejected = 0
        for ev in events:
            try:
                records.append(
                    to_record(
                        ev,
                        redactor=redactor,
                        retention=settings.retention,
                        trusted_user_keys=settings.trusted_user_keys,
                        now=now(),
                    )
                )
            except PayloadError as err:
                rejected += 1
                log.error("rejected event: %s", err)  # never log content
        try:
            inserted = repo.insert(records)
        except StoreUnavailable as err:
            log.error("store unavailable, %d records not stored: %s", len(records), err)
            metrics.inc("store_errors_total")
            return 503, {"error": "audit store unavailable"}
        metrics.inc("records_received_total", n=len(events))
        metrics.inc("records_inserted_total", n=inserted)
        metrics.inc("records_duplicate_total", n=len(records) - inserted)
        metrics.inc("records_rejected_total", n=rejected)
        for r in records:
            for kind, n in r.redactions.items():
                metrics.inc("redactions_applied_total", f'type="{kind}"', n)
            for flag in r.flags:
                metrics.inc("flags_raised_total", f'flag="{flag}"')
        log.info("stored batch: received=%d inserted=%d rejected=%d", len(events), inserted, rejected)
        return 200, {
            "received": len(events),
            "inserted": inserted,
            "duplicates": len(records) - inserted,
            "rejected": rejected,
        }

    @app.post("/v1/events")
    async def events(request: Request) -> JSONResponse:
        if not _authorised(request.headers.get("authorization"), settings.token):
            metrics.inc("requests_total", 'code="401"')
            return JSONResponse({"error": "unauthorised"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            metrics.inc("requests_total", 'code="413"')
            return JSONResponse({"error": "body too large"}, status_code=413)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_body_bytes:
                metrics.inc("requests_total", 'code="413"')
                return JSONResponse({"error": "body too large"}, status_code=413)
            chunks.append(chunk)
        code, result = await run_in_threadpool(process, b"".join(chunks))
        metrics.inc("requests_total", f'code="{code}"')
        return JSONResponse(result, status_code=code)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            repo.ping()
        except StoreUnavailable:
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    def prometheus() -> PlainTextResponse:
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    return app

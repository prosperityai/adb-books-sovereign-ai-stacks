"""Where audit records go: Postgres in production, memory in tests.

Book: Sovereign AI Stacks, Chapter 10

Both repositories have the same contract: insert() is idempotent on call_id
and returns how many records were new. The Postgres repository connects as
audit_writer, which holds INSERT and nothing else, so a compromised sink can
add records but cannot read, rewrite or delete any.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Protocol

from audit.models import AuditRecord


class StoreUnavailable(Exception):
    """The database cannot take writes right now (the caller should retry)."""


class Repository(Protocol):
    def insert(self, records: Sequence[AuditRecord]) -> int: ...

    def ping(self) -> None: ...


class MemoryRepository:
    """Thread-safe, dict-backed repository with the same semantics as Postgres."""

    def __init__(self) -> None:
        self.rows: dict[str, AuditRecord] = {}
        self._lock = threading.Lock()
        self.fail = False  # tests flip this to simulate an outage

    def insert(self, records: Sequence[AuditRecord]) -> int:
        if self.fail:
            raise StoreUnavailable("memory store marked unavailable")
        new = 0
        with self._lock:
            for r in records:
                if r.call_id not in self.rows:
                    self.rows[r.call_id] = r
                    new += 1
        return new

    def ping(self) -> None:
        if self.fail:
            raise StoreUnavailable("memory store marked unavailable")


EVENT_COLUMNS = (
    "call_id", "response_id", "trace_id", "event_time", "end_time", "status", "call_type",
    "model", "model_group", "tenant", "team_id", "key_alias", "key_hash", "end_user",
    "end_user_verified", "requester_ip", "prompt_tokens", "completion_tokens", "redactions",
    "flags", "retain_until",
)  # fmt: skip
CONTENT_COLUMNS = (
    "call_id", "tenant", "event_time", "messages", "response", "error", "content_sha256", "retain_until",
)  # fmt: skip


def insert_sql(table: str, columns: tuple[str, ...]) -> str:
    # No conflict target on purpose: "ON CONFLICT (call_id)" needs SELECT on
    # call_id, and the writer has none. call_id is the only unique key, so
    # the bare form deduplicates on it all the same.
    return (
        f"INSERT INTO audit.{table} ({', '.join(columns)}) "  # noqa: S608 (fixed identifiers, no user input)
        f"VALUES ({', '.join(['%s'] * len(columns))}) ON CONFLICT DO NOTHING"
    )


EVENTS_SQL = insert_sql("events", EVENT_COLUMNS)
CONTENT_SQL = insert_sql("event_content", CONTENT_COLUMNS)


class PostgresRepository:
    """psycopg 3 connection pool against the audit-db CloudNativePG cluster."""

    def __init__(self, conninfo: str, min_size: int = 1, max_size: int = 8) -> None:
        from psycopg_pool import ConnectionPool  # imported here: tests need no libpq

        self.pool = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"application_name": "audit-sink", "connect_timeout": 5},
        )

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    @staticmethod
    def _rows(r: AuditRecord) -> tuple[tuple, tuple]:
        from psycopg.types.json import Jsonb

        event = (
            r.call_id, r.response_id, r.trace_id, r.event_time, r.end_time, r.status, r.call_type,
            r.model, r.model_group, r.tenant, r.team_id, r.key_alias, r.key_hash, r.end_user,
            r.end_user_verified, r.requester_ip, r.prompt_tokens, r.completion_tokens,
            Jsonb(r.redactions), r.flags, r.retain_until,
        )  # fmt: skip
        content = (
            r.call_id, r.tenant, r.event_time, Jsonb(r.messages), Jsonb(r.response), r.error,
            r.content_sha256, r.content_retain_until,
        )  # fmt: skip
        return event, content

    def insert(self, records: Sequence[AuditRecord]) -> int:
        import psycopg
        from psycopg_pool import PoolTimeout

        if not records:
            return 0
        try:
            # One transaction per batch: either the whole batch is stored, or
            # none of it is and LiteLLM's retry (or the next flush) resends it.
            with self.pool.connection(timeout=10) as conn, conn.transaction(), conn.cursor() as cur:
                rows = [self._rows(r) for r in records]
                cur.executemany(EVENTS_SQL, [e for e, _ in rows])
                inserted = max(cur.rowcount, 0)
                cur.executemany(CONTENT_SQL, [c for _, c in rows])
                return inserted
        except (psycopg.OperationalError, PoolTimeout) as err:
            raise StoreUnavailable(str(err)) from None

    def ping(self) -> None:
        import psycopg
        from psycopg_pool import PoolTimeout

        try:
            with self.pool.connection(timeout=3) as conn:
                conn.execute("SELECT 1")
        except (psycopg.OperationalError, PoolTimeout) as err:
            raise StoreUnavailable(str(err)) from None

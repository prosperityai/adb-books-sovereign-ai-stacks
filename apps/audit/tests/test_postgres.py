"""The real database contract: append-only grants, RLS, idempotency, retention.

Runs against a disposable PostgreSQL 16+ server when AUDIT_TEST_ADMIN_DSN
points at it with a superuser, e.g.
    AUDIT_TEST_ADMIN_DSN="host=/var/run/postgresql user=postgres" pytest apps/audit/tests
It creates and drops a database called audit_test. Skipped otherwise.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from audit.models import RetentionPolicy, to_record
from audit.redact import Redactor
from conftest import event

ADMIN = os.environ.get("AUDIT_TEST_ADMIN_DSN")
pytestmark = pytest.mark.skipif(not ADMIN, reason="AUDIT_TEST_ADMIN_DSN not set")
MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"
DB = "audit_test"


def dsn(user: str) -> str:
    return f"{ADMIN} dbname={DB} user={user}"


def rec(call_id: str, tenant: str, start: datetime, metadata: int = 30, content: int = 30):
    ev = event(litellm_call_id=call_id, startTime=start.timestamp(), metadata={"user_api_key_team_alias": tenant})
    return to_record(
        ev,
        redactor=Redactor(),
        retention=RetentionPolicy.from_json(f'{{"*": {{"metadata": {metadata}, "content": {content}}}}}'),
        trusted_user_keys=frozenset({"chat-ministry-health"}),
    )


@pytest.fixture(scope="module")
def db():
    import psycopg

    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'audit_owner'").fetchone():
            c.execute("CREATE ROLE audit_owner NOLOGIN")
        c.execute(f"CREATE DATABASE {DB} OWNER audit_owner")
    with psycopg.connect(f"{ADMIN} dbname={DB}", autocommit=True) as c:
        # Executed as one plain-SQL string, exactly as CloudNativePG does.
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        c.execute(MIGRATION.read_text(encoding="utf-8"))  # idempotent
        for role in ("audit_writer", "audit_purger"):
            c.execute(f"ALTER ROLE {role} LOGIN")
        for role, group in (("dpo_mh", "audit_reader"), ("ext_auditor", "audit_auditor")):
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                c.execute(f"CREATE ROLE {role} LOGIN IN ROLE {group}")
        c.execute("INSERT INTO audit.reader_scopes VALUES ('dpo_mh', 'ministry-health') ON CONFLICT DO NOTHING")
    yield
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")


@pytest.fixture(scope="module")
def writer(db):
    from audit.store import PostgresRepository

    repo = PostgresRepository(dsn("audit_writer"), max_size=2)
    repo.open()
    yield repo
    repo.close()


def test_writer_inserts_idempotently(writer) -> None:
    now = datetime.now(UTC)
    batch = [rec("pg-1", "ministry-health", now), rec("pg-2", "revenue-authority", now)]
    assert writer.insert(batch) == 2
    assert writer.insert(batch) == 0
    assert writer.insert([rec("pg-3", "ministry-health", now), batch[0]]) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM audit.events",
        "SELECT call_id FROM audit.event_content",
        "UPDATE audit.events SET model = 'x'",
        "UPDATE audit.event_content SET messages = NULL",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
        "SELECT audit.purge_expired()",
    ],
)
def test_writer_can_do_nothing_but_insert(writer, sql: str) -> None:
    import psycopg

    with psycopg.connect(dsn("audit_writer")) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(sql)


def test_even_a_superuser_cannot_rewrite_history(writer) -> None:
    import psycopg

    with psycopg.connect(f"{ADMIN} dbname={DB}") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            c.execute("UPDATE audit.events SET end_user = 'someone-else' WHERE call_id = 'pg-1'")
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            c.execute("DELETE FROM audit.events WHERE call_id = 'pg-1'")


def test_tenant_officer_sees_only_own_tenant(writer) -> None:
    import psycopg

    with psycopg.connect(dsn("dpo_mh")) as c:
        tenants = {r[0] for r in c.execute("SELECT DISTINCT tenant FROM audit.events")}
        assert tenants == {"ministry-health"}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO audit.reader_scopes VALUES ('dpo_mh', 'revenue-authority')")
    with psycopg.connect(dsn("ext_auditor")) as c:
        tenants = {r[0] for r in c.execute("SELECT DISTINCT tenant FROM audit.events")}
        assert tenants == {"ministry-health", "revenue-authority"}


def test_purge_respects_both_clocks_and_legal_hold(writer) -> None:
    import psycopg

    old = datetime.now(UTC) - timedelta(days=400)
    writer.insert(
        [
            rec("old-ra", "revenue-authority", old),  # both clocks expired
            rec("old-mh-held", "ministry-health", old),  # expired, but under legal hold
            # Content expired, metadata still within its 2555 days:
            rec("old-mh-meta", "ministry-health", old + timedelta(days=10), metadata=2555, content=30),
        ]
    )
    with psycopg.connect(f"{ADMIN} dbname={DB}", autocommit=True) as c:
        c.execute(
            "INSERT INTO audit.legal_holds (tenant, from_time, to_time, reason) VALUES (%s, %s, %s, %s)",
            ("ministry-health", old - timedelta(hours=1), old + timedelta(seconds=1), "Inquiry 2026/14"),
        )
    with psycopg.connect(dsn("audit_purger"), autocommit=True) as c:
        purged = sorted(c.execute("SELECT * FROM audit.purge_expired()").fetchall())
        assert purged == [
            ("event_content", "ministry-health", 1),  # old-mh-meta: content clock ran out
            ("event_content", "revenue-authority", 1),
            ("events", "revenue-authority", 1),
        ]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT count(*) FROM audit.events")
    with psycopg.connect(dsn("ext_auditor")) as c:
        rows = dict(c.execute("SELECT call_id, messages IS NOT NULL FROM audit.requests").fetchall())
        assert "old-ra" not in rows
        assert rows["old-mh-held"] is True  # legal hold: content kept
        assert rows["old-mh-meta"] is False  # who and when kept, what is gone
        log = c.execute("SELECT tbl, tenant, rows_deleted FROM audit.purge_log ORDER BY tbl, tenant").fetchall()
        assert log == [
            ("event_content", "ministry-health", 1),
            ("event_content", "revenue-authority", 1),
            ("events", "revenue-authority", 1),
        ]

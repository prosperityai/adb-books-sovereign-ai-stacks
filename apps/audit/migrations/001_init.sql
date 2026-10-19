-- =============================================================================
-- 001_init.sql — the audit log: append-only, tenant-scoped, retention-bound
-- Book: Sovereign AI Stacks, Chapter 10 (threats T8, T9)
-- PostgreSQL 18 (CloudNativePG cluster audit-db); also tested on 16.
--
-- Run once as a superuser in database "audit". On CloudNativePG this happens
-- automatically through bootstrap.initdb.postInitApplicationSQLRefs (see
-- platform/security/audit-sink.yaml), which runs plain SQL, so this file has
-- no psql meta-commands. By hand: psql -v ON_ERROR_STOP=1 -d audit -f 001_init.sql
-- It is idempotent: re-running it restores exactly these objects and grants.
--
-- Two tables, two retention clocks. audit.events holds who, when, which key,
-- which model and how many tokens: small, and kept for the long period the
-- regulator sets. audit.event_content holds the redacted prompt and answer:
-- the sensitive part, kept only as long as it is needed. "Who asked what on
-- 3 March" is answerable for as long as both exist; "who used the model on
-- 3 March" for as long as the metadata exists.
--
-- Roles and what each may do:
--   audit_owner    owns every object. Nothing runs as it day to day.
--   audit_writer   the audit sink. INSERT only: it can never read, change or
--                  delete what it wrote.
--   audit_purger   the nightly retention job. EXECUTE on purge_expired() only.
--   audit_reader   group for tenant data-protection officers. SELECT, limited
--                  by row-level security to the tenants in reader_scopes.
--   audit_auditor  group for external auditors. SELECT on every tenant.
--   audit_access_logged  holds no members; pgaudit logs every statement that
--                  touches a table it has a privilege on (reads of the log).
-- Grant people the group roles; never give them the writer or purger.
-- =============================================================================
SET client_min_messages = warning;

DO $roles$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['audit_owner', 'audit_writer', 'audit_purger', 'audit_reader',
                           'audit_auditor', 'audit_access_logged']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      -- NOLOGIN here; CloudNativePG's managed roles add LOGIN and a password
      -- for the two service roles.
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END
$roles$;

-- Object-level audit of reads, if the server has pgaudit (the CloudNativePG
-- standard image does; audit-sink.yaml sets pgaudit.role).
DO $pgaudit$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgaudit') THEN
    CREATE EXTENSION IF NOT EXISTS pgaudit;
  END IF;
END
$pgaudit$;

CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION audit_owner;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

SET ROLE audit_owner;

-- One row per LiteLLM call. call_id is LiteLLM's litellm_call_id: LiteLLM may
-- resend a batch, and ON CONFLICT DO NOTHING turns a resend into a no-op.
-- Keep call_id the ONLY unique constraint on both tables: the writer's bare
-- ON CONFLICT relies on it (a conflict target would need SELECT).
CREATE TABLE IF NOT EXISTS audit.events (
    call_id            text        PRIMARY KEY,
    response_id        text,
    trace_id           text,
    event_time         timestamptz NOT NULL,
    end_time           timestamptz,
    received_at        timestamptz NOT NULL DEFAULT now(),
    status             text        NOT NULL CHECK (status IN ('success', 'failure')),
    call_type          text        NOT NULL,
    model              text        NOT NULL,
    model_group        text,
    tenant             text        NOT NULL,
    team_id            text,
    key_alias          text        NOT NULL,
    key_hash           text,
    end_user           text,
    end_user_verified  boolean     NOT NULL,
    requester_ip       inet,
    prompt_tokens      integer     NOT NULL DEFAULT 0,
    completion_tokens  integer     NOT NULL DEFAULT 0,
    redactions         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    flags              text[]      NOT NULL DEFAULT '{}',
    retain_until       timestamptz NOT NULL,
    CHECK (retain_until > event_time)
);

CREATE TABLE IF NOT EXISTS audit.event_content (
    call_id         text        PRIMARY KEY REFERENCES audit.events (call_id),
    tenant          text        NOT NULL,
    event_time      timestamptz NOT NULL,
    messages        jsonb,
    response        jsonb,
    error           text,
    content_sha256  text        NOT NULL,
    retain_until    timestamptz NOT NULL,
    CHECK (retain_until > event_time)
);

-- The two questions auditors ask: what did tenant X do on day D, and what
-- did person P do on day D.
CREATE INDEX IF NOT EXISTS events_tenant_time ON audit.events (tenant, event_time);
CREATE INDEX IF NOT EXISTS events_user_time ON audit.events (end_user, event_time);
CREATE INDEX IF NOT EXISTS events_retain_until ON audit.events (retain_until);
CREATE INDEX IF NOT EXISTS content_retain_until ON audit.event_content (retain_until);

-- Records under legal hold are never purged, whatever their retention says.
CREATE TABLE IF NOT EXISTS audit.legal_holds (
    hold_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant      text        NOT NULL,
    from_time   timestamptz NOT NULL,
    to_time     timestamptz NOT NULL,
    reason      text        NOT NULL,
    created_by  text        NOT NULL DEFAULT session_user,
    created_at  timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    CHECK (to_time > from_time)
);

-- Evidence that retention ran: one row per tenant and table per purge, and a
-- '*' row when a run deleted nothing.
CREATE TABLE IF NOT EXISTS audit.purge_log (
    run_at       timestamptz NOT NULL DEFAULT now(),
    tbl          text        NOT NULL,
    tenant       text        NOT NULL,
    rows_deleted bigint      NOT NULL
);

-- Which tenants each data-protection officer's login may read.
CREATE TABLE IF NOT EXISTS audit.reader_scopes (
    role_name text NOT NULL,
    tenant    text NOT NULL,
    PRIMARY KEY (role_name, tenant)
);

-- ---------------------------------------------------------------------------
-- Append-only enforcement. Grants already deny UPDATE and DELETE to every
-- service role; the triggers also stop the owner and superusers, so a
-- mistaken maintenance session cannot rewrite history. Only purge_expired()
-- may delete, and only rows past retention.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('audit.purge_in_progress', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END
$fn$;

DO $triggers$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['events', 'event_content'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS append_only ON audit.%I', t);
    EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON audit.%I '
                   'FOR EACH ROW EXECUTE FUNCTION audit.forbid_change()', t);
    EXECUTE format('DROP TRIGGER IF EXISTS no_truncate ON audit.%I', t);
    EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON audit.%I '
                   'FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_change()', t);
  END LOOP;
END
$triggers$;

-- ---------------------------------------------------------------------------
-- Retention. SECURITY DEFINER runs as audit_owner, so the purger needs no
-- table privileges at all. Content goes first (its clock is shorter), then
-- metadata whose content is already gone. Returns what it did, and logs it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.purge_expired()
RETURNS TABLE (purged_table text, purged_tenant text, purged_rows bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $fn$
DECLARE
  total bigint := 0;
BEGIN
  PERFORM set_config('audit.purge_in_progress', 'on', true);

  FOR purged_table, purged_tenant, purged_rows IN
    WITH gone AS (
      DELETE FROM audit.event_content c
      WHERE c.retain_until < now()
        AND NOT EXISTS (
          SELECT 1 FROM audit.legal_holds h
          WHERE h.released_at IS NULL AND h.tenant = c.tenant
            AND c.event_time >= h.from_time AND c.event_time < h.to_time)
      RETURNING c.tenant
    )
    SELECT 'event_content', g.tenant, count(*)::bigint FROM gone g GROUP BY g.tenant
  LOOP
    INSERT INTO audit.purge_log (tbl, tenant, rows_deleted) VALUES (purged_table, purged_tenant, purged_rows);
    total := total + purged_rows;
    RETURN NEXT;
  END LOOP;

  FOR purged_table, purged_tenant, purged_rows IN
    WITH gone AS (
      DELETE FROM audit.events e
      WHERE e.retain_until < now()
        AND NOT EXISTS (SELECT 1 FROM audit.event_content c WHERE c.call_id = e.call_id)
        AND NOT EXISTS (
          SELECT 1 FROM audit.legal_holds h
          WHERE h.released_at IS NULL AND h.tenant = e.tenant
            AND e.event_time >= h.from_time AND e.event_time < h.to_time)
      RETURNING e.tenant
    )
    SELECT 'events', g.tenant, count(*)::bigint FROM gone g GROUP BY g.tenant
  LOOP
    INSERT INTO audit.purge_log (tbl, tenant, rows_deleted) VALUES (purged_table, purged_tenant, purged_rows);
    total := total + purged_rows;
    RETURN NEXT;
  END LOOP;

  -- A run that deleted nothing is evidence too: retention is running.
  IF total = 0 THEN
    INSERT INTO audit.purge_log (tbl, tenant, rows_deleted) VALUES ('*', '*', 0);
  END IF;
  PERFORM set_config('audit.purge_in_progress', 'off', true);
END
$fn$;

-- The tenants the calling login may read (used by row-level security).
CREATE OR REPLACE FUNCTION audit.my_tenants() RETURNS SETOF text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, audit AS $fn$
  SELECT s.tenant FROM audit.reader_scopes s WHERE s.role_name = session_user
$fn$;

-- ---------------------------------------------------------------------------
-- Row-level security: a tenant's officer sees only that tenant's rows.
-- ---------------------------------------------------------------------------
DO $rls$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['events', 'event_content'] LOOP
    EXECUTE format('ALTER TABLE audit.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS writer_insert ON audit.%I', t);
    EXECUTE format('CREATE POLICY writer_insert ON audit.%I FOR INSERT TO audit_writer WITH CHECK (true)', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_read ON audit.%I', t);
    EXECUTE format('CREATE POLICY tenant_read ON audit.%I FOR SELECT TO audit_reader '
                   'USING (tenant IN (SELECT audit.my_tenants()))', t);
    EXECUTE format('DROP POLICY IF EXISTS auditor_read ON audit.%I', t);
    EXECUTE format('CREATE POLICY auditor_read ON audit.%I FOR SELECT TO audit_auditor USING (true)', t);
  END LOOP;
END
$rls$;

-- One view for the question the war story starts with.
CREATE OR REPLACE VIEW audit.requests WITH (security_invoker = true) AS
SELECT e.event_time, e.tenant, e.end_user, e.end_user_verified, e.key_alias, e.model,
       e.status, e.prompt_tokens, e.completion_tokens, e.flags, e.redactions,
       c.messages, c.response, c.error, e.call_id
FROM audit.events e
LEFT JOIN audit.event_content c USING (call_id);

-- ---------------------------------------------------------------------------
-- Privileges. Everything is revoked first, so re-running this file after a
-- manual grant puts the database back to exactly this state.
-- ---------------------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA audit FROM PUBLIC, audit_writer, audit_purger,
  audit_reader, audit_auditor, audit_access_logged;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM PUBLIC;

GRANT USAGE ON SCHEMA audit TO audit_writer, audit_purger, audit_reader, audit_auditor;
GRANT INSERT ON audit.events, audit.event_content TO audit_writer;
GRANT EXECUTE ON FUNCTION audit.purge_expired() TO audit_purger;
GRANT SELECT ON audit.events, audit.event_content, audit.requests, audit.purge_log, audit.legal_holds
  TO audit_reader, audit_auditor;
GRANT EXECUTE ON FUNCTION audit.my_tenants() TO audit_reader;
GRANT SELECT ON audit.events, audit.event_content TO audit_access_logged;

RESET ROLE;

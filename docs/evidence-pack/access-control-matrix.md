# Access-Control Matrix

Companion to *Sovereign AI Stacks*, Chapter 10. One row per principal, one column per data class. Every cell names the mechanism that enforces it, because "by policy" is not a control. Chapter 8's rule applies throughout: a store of derived data inherits the access rules of its inputs, so the audit log is restricted at least as tightly as the prompts it records.

Legend: **R** read, **W** write or append, **D** delete, **—** no access.

| Principal | Prompts in flight | Audit metadata (`audit.events`) | Audit content (`audit.event_content`) | Tenant documents (Qdrant) | Model weights | Enforced by |
|---|---|---|---|---|---|---|
| End user (Keycloak, tenant group) | Own session only | — | — | Per ACL claims | — | OIDC client per tenant UI; RAG ACL filter (Ch 7, 8) |
| Tenant application key | Own requests | — | — | — | — | LiteLLM virtual key, team model list (Ch 6) |
| Audit sink (`audit_writer`) | Receives copy | W | W | — | — | PostgreSQL grants: INSERT only; append-only trigger |
| Retention job (`audit_purger`) | — | D (expired only) | D (expired only) | — | — | EXECUTE on `audit.purge_expired()` only |
| Tenant DPO (member of `audit_reader`) | — | R, own tenant | R, own tenant | — | — | Row-level security via `audit.reader_scopes`; pgaudit logs every read |
| External auditor (member of `audit_auditor`) | — | R, all tenants | R, all tenants | — | — | RLS policy `auditor_read`; login `VALID UNTIL` the audit's end date; pgaudit |
| Platform administrator | — | — (break-glass only) | — (break-glass only) | — (break-glass only) | R | Kubernetes RBAC; `audit-db-app` owner secret held by security officer; no superuser login (`enableSuperuserAccess: false`) |
| Platform security officer | — | Legal holds | — | — | — | Owner role, break-glass, two-person rule |
| vLLM pods | Process | — | — | — | R (read-only mount) | `readOnly: true` volume; Falco write rule |
| Model fetch init container | — | — | — | — | W (once, hash-checked) | `oci_pull.py` manifest check (Ch 5) |
| Any pod in a data namespace | — | — | — | — | — | No service-account token; Falco token-read rule |

## Break-glass procedure

1. The requester opens a ticket naming the tenant, the time range and the reason.
2. The platform security officer and the tenant's data-protection officer both approve.
3. The officer creates a time-limited login: `CREATE ROLE bg_<ticket> LOGIN PASSWORD '...' VALID UNTIL '<+24h>' IN ROLE audit_reader;` and adds one `audit.reader_scopes` row for the named tenant.
4. pgaudit records every statement the login runs. The officer attaches that log extract to the ticket.
5. The role is dropped when the ticket closes. The next evidence pack's `audit-db/table-grants.csv` shows it gone.

## Quarterly access review

| Date | Reviewer | Roles reviewed | Removed | Notes |
|---|---|---|---|---|
| | | | | |

# Retention Policy — Prompt and Response Audit Log

Companion to *Sovereign AI Stacks*, Chapter 10. The figures below are the example values shipped in `platform/security/audit-sink.yaml`. They are not legal advice. Agree each tenant's figures with its data-protection officer and counsel, then change the ConfigMap and this table in the same pull request.

## Two clocks

The audit log keeps two kinds of data for two different lengths of time.

- **Metadata** (`audit.events`): who asked, through which key, which model, when, how many tokens, what was redacted, any injection flag. It answers "who used the system on 3 March?" It's small and carries little personal data beyond the user's identity, so it can be kept for the regulator's full period.
- **Content** (`audit.event_content`): the redacted prompt and response. It answers "what did they ask?" It's the sensitive part, so it's kept only as long as investigations realistically need it.

Content can never outlive its metadata. The sink refuses a policy that says otherwise, and the purge function deletes content first.

| Tenant | Metadata (days) | Content (days) | Basis (to be confirmed by the tenant's DPO) |
|---|---|---|---|
| `ministry-health` | 2,555 (7 years) | 365 | Health-sector record-keeping; clinical queries reviewed within a year |
| `revenue-authority` | 3,650 (10 years) | 365 | Tax-administration audit trail |
| `platform` | 365 | 90 | Internal services (RAG, eval harness); operational only |
| any other (`*`) | 730 | 180 | Default for a tenant not yet profiled |

## How deletion happens and how it's proven

1. Each record is stamped with `retain_until` (metadata) and a separate `retain_until` on its content row when the sink stores it. The stamp uses the policy in force at collection time.
2. CronJob `audit-purge` runs `audit.purge_expired()` at 02:17 every night as `audit_purger`, a role that can do nothing else.
3. The function skips any record covered by an open row in `audit.legal_holds`.
4. Every run writes `audit.purge_log`: one row per table and tenant, or a single `*` row when nothing expired. The evidence pack exports the last 90 days (`audit-db/purge-log-90d.csv`). A missing night is a finding.
5. Backups (Chapter 12) keep their own copies for their own retention window, so a purged record is gone from every copy only once the last backup taken before the purge expires. Write that window here (`backup retention: ___ days`) and state it to the regulator; don't let them discover it.

## Changing a period

- **Longer:** applies to records collected after the change. Existing records keep their stamp.
- **Shorter:** applies to new records only, unless the DPO orders otherwise. To shorten existing records, run a reviewed one-off migration as the owner role, inside a ticket, and record it below.
- **Legal hold:** `INSERT INTO audit.legal_holds (tenant, from_time, to_time, reason) ...` as the owner role. Release by setting `released_at`. Holds and releases appear in `audit-db/legal-holds.csv`.

## Data-subject requests

The audit log exists to meet a legal obligation, which is usually a recognised exemption from erasure under the PDPA, the Kenyan DPA and the UAE PDPL. Confirm this with counsel for each tenant. Access requests are answered by the tenant's DPO from `audit.requests`, filtered to the subject's identity.

## Change log

| Date | Tenant | Change | Approved by |
|---|---|---|---|
| | | Initial policy | |

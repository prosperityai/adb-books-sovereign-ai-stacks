# Data-Flow Register

Companion to *Sovereign AI Stacks*, Chapter 10. Tanzania's PDPA 2022, Kenya's DPA 2019 and the UAE PDPL all expect the controller to know where personal data goes. This register lists every flow on the platform that can carry personal or classified data. A pull request that adds a flow, a store or an egress rule must update this file (Chapter 1's review rule).

Every flow below stays inside the enclave. No row has a destination outside it, and Cilium's baseline policy plus Kyverno's `deny-world-egress` make it impossible to add one without a reviewed policy change.

## Flows

| ID | From | To | Data | Transport | Stored at destination? | Control |
|---|---|---|---|---|---|---|
| F1 | User browser | Open WebUI (`chat-ui`) | Prompts, uploaded files, user identity | HTTPS via cilium ingress, OIDC session | Yes: chat history in the tenant's Open WebUI volume | Per-tenant UI release, Keycloak SSO (Ch 6, 7) |
| F2 | Open WebUI | LiteLLM (`ai-gateway`) | Prompts, `X-OpenWebUI-User-Email` | HTTP in-cluster, WireGuard between nodes | No (spend metadata only, `store_prompts_in_spend_logs: false`) | Virtual key per tenant UI (Ch 6) |
| F3 | RAG API (`rag`) | LiteLLM | Prompts with retrieved document chunks, user subject | HTTP in-cluster | No | `rag-api` key; ACL filter before generation (Ch 8) |
| F4 | LiteLLM | vLLM (`inference`) | Prompts, completions | HTTP in-cluster | No (KV cache in GPU memory only) | `model-servers-ingress` policy (Ch 5) |
| F5 | LiteLLM | Audit sink (`ai-gateway`) | Full request and response, key alias, team, end user | HTTP in-cluster, bearer token | Yes, after redaction: `audit.events`, `audit.event_content` | `audit-sink-ingress` L7 policy; redaction; RLS (Ch 10) |
| F6 | LiteLLM | Langfuse (`monitoring`) | Traces: timings, model, token counts, prompt and response | OTLP in-cluster | Yes: Langfuse (ClickHouse, S3 events), unredacted | In-enclave `LANGFUSE_HOST`; 30-day nightly deletion (Ch 9). Review whether traces need content at all |
| F7 | Ingestion job | Qdrant, TEI (`rag`) | Document text, embeddings, ACL labels | HTTP in-cluster | Yes: Qdrant collection per tenant | Per-tenant collections (Ch 8) |
| F8 | All pods | Loki (`monitoring`) | Application logs | In-cluster | Yes | Applications must not log prompts; the audit sink logs counts only |
| F9 | Falco | falcosidekick, Alertmanager | Alert text: pod, process, command line, destination IP | In-cluster | Alertmanager memory; Loki | Command lines can contain arguments: treat alerts as confidential |
| F10 | Databases, volumes | Backup appliance `backup.enclave.internal` | Everything above, encrypted | HTTPS to 10.20.0.40 | Yes, second room | Ch 12 |

## Stores holding personal data

| Store | Personal data | Redacted? | Retention | Owner |
|---|---|---|---|---|
| `audit.events` | End-user e-mail, key alias, IP, timings | Identity kept on purpose; content not stored here | Per tenant, metadata clock (`retention-policy.md`) | Platform security officer |
| `audit.event_content` | Prompts and responses | Yes, before storage (e-mail, phone, NIDA, card, IBAN) | Per tenant, content clock | Tenant data-protection officer |
| Open WebUI volume | Chat history, uploads | No | Tenant policy; user-deletable | Tenant |
| Qdrant | Document chunks | No (source documents are the tenant's records) | Life of the source document | Tenant data owner |
| Langfuse | Traces: full prompts and responses, end-user e-mail | No | 30 days, CronJob `langfuse-retention` (Ch 9; `retention-policy.md`) | Platform team |
| Loki | Logs | Must contain no content; e-mail masked by Alloy | 30 days, Loki compactor (Ch 9) | Platform team |

## Review

| Date | Reviewer | Change |
|---|---|---|
| | | Initial register |

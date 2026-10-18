# Sovereign AI Stacks — Companion Code

Companion repository for **_Sovereign AI Stacks: Deploying Air-Gapped LLMs for Government, Banking and Telecom_** by Prosperity Paul Kipingili.

Every manifest, script and service in the book lives here, organised by chapter. Each chapter's code is committed when that chapter is published, and it lints and passes its tests before it lands.

## Chapter map

| Chapter | Topic | Folders |
|---|---|---|
| 1 | Threat model and reference stack | `docs/`, `tools/preflight/` |
| 2 | GPU sizing | `tools/sizing/` |
| 3 | Air-gapped supply chain | `airgap/`, `platform/harbor/` |
| 4 | Offline cluster bootstrap | `infra/talos/`, `platform/cilium/`, `platform/gpu-operator/`, `platform/storage/` |
| 5 | Serving models with vLLM | `platform/inference/`, `tools/bench/` |
| 6 | AI gateway | `platform/gateway/`, `apps/gateway_admin/` |
| 7 | Identity and multi-tenancy | `platform/identity/`, `platform/policies/` |
| 8 | Sovereign RAG | `apps/rag/`, `platform/rag/` |
| 9 | Observability | `platform/observability/` |
| 10 | Security and compliance evidence | `platform/security/`, `apps/audit/`, `docs/evidence-pack/` |
| 11 | Evaluation and acceptance | `evals/` |
| 12 | Day-2 operations | `ops/` |

## Versions

See [`VERSIONS.md`](VERSIONS.md). Every pin is recorded with the date it was verified.

## Licence

Code is MIT-licensed (see `LICENSE`). The book text is © Prosperity Paul Kipingili, all rights reserved.

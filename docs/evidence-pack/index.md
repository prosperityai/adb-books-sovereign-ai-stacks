# Evidence Pack — Index

Companion to *Sovereign AI Stacks*, Chapter 10. This is the first page an auditor reads. It maps each control the platform claims to the evidence that proves it and the file in the pack where that evidence sits.

Build the pack with `docs/evidence-pack/collect_evidence.sh`. The tarball holds `SHA256SUMS` over every file (signed as `SHA256SUMS.sig` when you pass `--sign`), `manifest.json`, and `GAPS.txt`, which lists every piece of evidence the collector could not find. An empty `GAPS.txt` is the goal. A non-empty one is still honest.

| Pack directory | Contents | Produced by |
|---|---|---|
| `docs/` | This index, the registers below, the threat model | This repository |
| `config/` | Policies, Falco rules, audit schema, tenants, version pins, as deployed | This repository |
| `preflight/` | One preflight JSON per node, from installation day | `tools/preflight/check_host.sh --json` (Ch 1) |
| `supply-chain/` | `import-record*.json` per bundle; `signing-record*.jsonl` per import | `verify_bundle.sh` (Ch 3); `platform/security/sign_imported_images.sh` (Ch 10) |
| `models/` | One dossier per served model | `model-dossier-template.md`, filled in (Ch 5) |
| `cluster/` | Kyverno policies and policy reports, Cilium policies, running images by digest, Falco rules ConfigMap | `kubectl` at collection time |
| `audit-db/` | Purge log, request volume, redaction counts, table grants, legal holds | `psql` as an `audit_auditor` login |

## Control-to-evidence map

| Threat (docs/threat-model.md) | Control | Evidence in the pack |
|---|---|---|
| T1 Data leaves the enclave | Cilium default-deny egress; Kyverno `deny-world-egress`; Falco `Connection attempt outside the enclave` | `config/cilium/enclave-egress-baseline.yaml`, `cluster/cilium-*.yaml`, `preflight/*.json` (egress check), `cluster/policyreports.json` |
| T2 Model weights tampered | SHA-256 manifests, hash check at load; Falco `Model weights opened for writing` | `supply-chain/import-record*.json`, `models/*.md` (revision and hashes) |
| T3 Malicious image imported | Bundle signature (Ch 3); enclave admission signature; Kyverno `verify-images` | `supply-chain/signing-record*.jsonl`, `cluster/clusterpolicies.yaml`, `cluster/running-images.tsv`, `cluster/enclave-cosign-pub.yaml` |
| T4 Cross-tenant retrieval | Per-tenant collections, ACL filters (Ch 8) | Ch 8 lab matrix (attach as `docs/rag-acl-matrix.md`) |
| T8 Repudiation | Append-only audit log with key alias, claimed user, tenant, model, time | `config/001_init.sql`, `audit-db/volume-by-tenant-30d.csv`, `audit-db/table-grants.csv` |
| T9 PII in the audit log | Redaction before storage; RLS; pgaudit on reads; two retention clocks | `audit-db/redactions-30d.csv`, `audit-db/purge-log-90d.csv`, `access-control-matrix.md`, `retention-policy.md` |
| T12 Signing key compromise | Two keys, split custody, rotation runbook | `cluster/enclave-cosign-pub.yaml` (fingerprint matches `signing-record` `key_sha256`), key ceremony record |

## Documents

- [data-flow-register.md](data-flow-register.md): every flow of personal or classified data, where it is stored and for how long.
- [access-control-matrix.md](access-control-matrix.md): who can read, change or delete each class of data, and how that is enforced.
- [retention-policy.md](retention-policy.md): retention periods per tenant and data class, and how deletion is proven.
- [model-dossier-template.md](model-dossier-template.md): one per model served.
- [../threat-model.md](../threat-model.md): the STRIDE register this pack answers.

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Platform security officer | | | |
| Data-protection officer, Ministry of Health | | | |
| Data-protection officer, Revenue Authority | | | |

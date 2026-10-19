# Model Dossier — `<model alias>`

Companion to *Sovereign AI Stacks*, Chapters 5 and 10. Fill in one copy per model the gateway serves, save it as `models/<alias>.md` in the evidence pack, and update it whenever the revision, quantisation or serving flags change. Every field must be checkable against a file or a command output. Write "unknown" rather than leave a guess.

## 1. Identity

| Field | Value | Where it comes from |
|---|---|---|
| Gateway alias | `qwen3.8-27b` | `platform/gateway/litellm-values.yaml` |
| Publisher and repository | | `airgap/models.yaml` |
| Upstream revision (commit SHA) | | `airgap/models.yaml`, Harbor tag |
| Harbor reference (digest) | `harbor.enclave.internal/models/...@sha256:...` | `push_model_oci.sh` output (Ch 3) |
| Weights format and quantisation | e.g. FP8 | Model card, `config.json` |
| Checkpoint size | | `du -sh` on the model volume |
| MANIFEST.sha256 checked at load | yes / no | `oci_pull.py` log line in the model-fetch init container |

## 2. Licence and provenance

| Field | Value |
|---|---|
| Licence | e.g. Apache-2.0 |
| Licence file hash | `sha256sum LICENSE` from the bundle |
| Use restrictions | none / list |
| Training data statement from publisher | link or "not published" |
| Known fine-tuning on our data | none / dataset name and review reference |
| Bundle and import record | `supply-chain/import-record-<bundle>.json` |

## 3. Serving configuration

| Field | Value |
|---|---|
| vLLM version and image digest | |
| GPUs per replica, GPU type | |
| `--max-model-len` | |
| `--kv-cache-dtype` | |
| `--gpu-memory-utilization` | |
| Replicas | |
| Measured p95 TTFT / TPOT at sized concurrency | from Ch 5 benchmark report |

## 4. Evaluation (Chapter 11)

| Suite | Swahili | English | Arabic | Threshold | Pass? |
|---|---|---|---|---|---|
| Task accuracy | | | | | |
| Refusal and safety | | | | | |
| Prompt-injection red team | | | | | |
| Extraction test (T13) | | | | | |

## 5. Known limitations

List behaviours that users and approvers must know about: languages where quality drops, known failure modes, topics the model should not be used for.

-

## 6. Approval

| Role | Name | Date |
|---|---|---|
| Platform team lead | | |
| Platform security officer | | |
| Business owner (tenant) | | |

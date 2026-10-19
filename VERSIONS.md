# Version Pins

Every version the book and this repository depend on, with the date it was last checked against the upstream release page. When you upgrade a component, change it here, in the manifest or values file that uses it, and in `airgap/zarf.yaml`, all in the same pull request.

## Platform

| Component | Pin | Chapter | Verified |
|---|---|---|---|
| Kubernetes | 1.37.1 | 4, 12 | 2026-10-19 |
| Talos Linux / talosctl | 1.14.4 | 4, 12 | 2026-10-19 |
| Cilium (chart) / cilium CLI / hubble CLI | 1.20.3 / 0.20.1 / 1.20.2 | 4 | 2026-10-19 |
| NVIDIA driver (Talos extension) | 595.91.07 | 4 | 2026-10-19 |
| NVIDIA device plugin (chart) | 0.20.1 | 4 | 2026-10-19 |
| DCGM exporter (chart / image) | 4.8.4 / 4.6.1-4.8.4-distroless | 4, 9 | 2026-10-19 |
| Rook-Ceph (charts) | v1.21.0 | 4 | 2026-10-19 |
| Ceph-CSI drivers (chart; operator 1.1.0 ships with Rook) | 1.1.0 | 4 | 2026-10-19 |
| Harbor | 2.15.4 | 3 | 2026-10-19 |
| Zarf | v0.88.0 | 3 | 2026-10-19 |
| cosign / syft / oras / trivy | 2.6.5 / 1.54.1 / 1.3.5 / 0.76.0 | 3, App. A | 2026-10-19 |
| Helm | 3.22.1 | all | 2026-10-19 |

## Models and inference

| Component | Pin | Chapter | Verified |
|---|---|---|---|
| vLLM (`vllm/vllm-openai`) | v0.32.0 | 5 | 2026-10-19 |
| Qwen3.8-27B | Apache-2.0, revision pinned in `airgap/models.yaml` | 5 | 2026-10-19 |
| gpt-oss-120b | Apache-2.0, revision pinned in `airgap/models.yaml` | 5 | 2026-10-19 |
| Gemma 4 | Apache-2.0 (budget profile) | 2, 5 | 2026-10-19 |
| Text Embeddings Inference | cpu-1.9 | 8 | 2026-10-19 |
| BGE-M3, BGE reranker | revisions pinned in `airgap/models.yaml` | 8 | 2026-10-19 |

## Gateway, identity, data

| Component | Pin | Chapter | Verified |
|---|---|---|---|
| LiteLLM (image / chart) | v1.106.0 / 1.106.0 | 6 | 2026-10-19 |
| Open WebUI (image / chart) | v0.12.3 / 17.0.0 | 6 | 2026-10-19 |
| CloudNativePG (operator / chart / kubectl plugin) | 1.30.1 / 0.29.1 / 1.30.1 | 6 | 2026-10-19 |
| PostgreSQL | 18.6 (`18.6-standard-trixie`) | 6, 7, 10 | 2026-10-19 |
| Keycloak (server and operator) | 26.8.1 | 7 | 2026-10-19 |
| Kyverno (app / chart / CLI) | 1.19.1 / 3.9.1 / 1.19.1 | 7, 10 | 2026-10-19 |
| Qdrant (image / chart) | v1.19.2 / 1.19.2 | 8 | 2026-10-19 |
| Docling | 2.140.0 | 8 | 2026-10-19 |

## Observability (Chapter 9)

| Component | Pin | Verified |
|---|---|---|
| kube-prometheus-stack (chart) | 92.10.1 (Prometheus v3.15.0, Alertmanager v0.34.1, Grafana 13.2.4) | 2026-10-19 |
| Loki (chart / image) | 7.3.0 / 3.6.12 | 2026-10-19 |
| Grafana Alloy (chart / app) | 1.13.1 / v1.20.1 | 2026-10-19 |
| Langfuse (chart / app) | 2.1.5 / 4.56.0 | 2026-10-19 |
| ClickHouse operator (chart) | 0.0.8 | 2026-10-19 |
| Valkey | 8.0.11 | 2026-10-19 |

## Security and audit (Chapter 10)

| Component | Pin | Verified |
|---|---|---|
| Falco (chart / app) | 9.2.0 / 0.45.0 | 2026-10-19 |
| falcosidekick | 2.35.0 | 2026-10-19 |
| psycopg[binary] / psycopg-pool | 3.3.6 / 3.3.3 | 2026-10-19 |

## Evaluation (Chapter 11)

| Component | Pin | Verified |
|---|---|---|
| httpx | 0.28.1 | 2026-10-19 |
| PyYAML | 6.0.3 | 2026-10-19 |
| actions/checkout / setup-python / upload-artifact | v7.0.1 / v7.0.0 / v7.0.2 | 2026-10-19 |

## Backup and operations (Chapter 12)

| Component | Pin | Verified |
|---|---|---|
| Velero (chart / server / CLI) | 12.2.1 / v1.18.4 / 1.18.4 | 2026-10-19 |
| velero-plugin-for-aws | v1.14.4 | 2026-10-19 |
| Barman Cloud plugin (app / chart) | v0.15.1 / 0.8.1 | 2026-10-19 |

## Python services and tooling

| Component | Pin | Verified |
|---|---|---|
| Python | 3.13 (images `python:3.13-slim-trixie`, jobs `python:3.13.16-slim-trixie`) | 2026-10-19 |
| FastAPI / uvicorn / pydantic | 0.146.0 / 0.54.0 / 2.14.1 | 2026-10-19 |
| qdrant-client / PyJWT | 1.19.1 / 2.15.1 | 2026-10-19 |
| pytest / ruff / yamllint / shellcheck-py | 9.1.1 / 0.17.1 / 1.38.0 / 0.11.0.1 | 2026-10-19 |

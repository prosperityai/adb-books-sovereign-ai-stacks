# Sovereign AI Stacks: Companion Code

This is the companion repository for **_Sovereign AI Stacks: Deploying Air-Gapped LLMs for Government, Banking and Telecom_** by Prosperity Paul Kipingili (ADB Books, 2026).

Every manifest, values file, script and service the book uses is here, organised by the chapter that introduces it. The book prints these files from this repository at build time, so they are byte-for-byte identical. Every file is linted, and every piece of logic has tests (`make check`).

## What you build

The finished platform is an air-gapped, multi-tenant LLM platform. It has:

- open-weight models served by vLLM on Talos Linux and Kubernetes 1.37;
- one metered OpenAI-compatible gateway (LiteLLM);
- per-agency identity (Keycloak), with admission policy (Kyverno) and default-deny networking (Cilium);
- tenant-isolated RAG over Qdrant;
- self-hosted metrics, logs and traces;
- a redacted audit trail;
- evaluation gates in Swahili, Arabic and English;
- tested backups and disaster recovery.

Nothing in it needs the internet once it is installed.

## Chapter map

| Chapter | Topic | Where the code lives |
|---|---|---|
| 1 | What "sovereign" means, threat model | `tools/preflight/`, `docs/threat-model.md`, `platform/policies/chat-ui-egress.yaml` |
| 2 | Sizing GPUs | `tools/sizing/` |
| 3 | The air-gapped supply chain | `airgap/` (Zarf package, model fetch, signing, media split, verification, Harbor) |
| 4 | Bootstrapping the cluster offline | `infra/talos/`, `platform/base/`, `platform/cilium/`, `platform/gpu/`, `platform/storage/` |
| 5 | Serving models with vLLM | `platform/inference/`, `tools/bench/` |
| 6 | The AI gateway | `platform/gateway/`, `platform/chat-ui/`, `apps/gateway_admin/` |
| 7 | Identity, multi-tenancy and policy | `platform/identity/`, `platform/policies/` |
| 8 | Sovereign RAG | `apps/rag/`, `platform/rag/` |
| 9 | Observability without SaaS | `platform/observability/` |
| 10 | Security hardening and compliance evidence | `platform/security/`, `platform/policies/verify-images.yaml`, `apps/audit/`, `docs/evidence-pack/` |
| 11 | Evaluation and acceptance testing | `evals/`, `platform/evals/`, `.github/workflows/evals.yml` |
| 12 | Day-2 operations | `ops/` (backups, upgrades, DR drills, runbooks) |

## Quick start

```console
$ git clone https://github.com/prosperityai/adb-books-sovereign-ai-stacks.git
$ cd adb-books-sovereign-ai-stacks
$ python3.13 -m venv .venv && . .venv/bin/activate
$ pip install ruff==0.17.1 yamllint==1.38.0 shellcheck-py==0.11.0.1 pytest==9.1.1 pyyaml==6.0.3 httpx==0.28.1
$ pip install "./apps/rag[test]" "./apps/audit[test]" "./evals[test]" "./ops[test]"
$ make check
$ ./tools/preflight/check_host.sh --profile lab
```

`make check` needs no cluster and no GPU. Appendix A of the book covers the full toolchain (kubectl, helm, talosctl, zarf, cosign and the rest) for macOS, Ubuntu and WSL2.

| Target | What it does |
|---|---|
| `make lint` | ruff, shellcheck and yamllint over the whole repository |
| `make test` | every pytest suite, each run from its own directory |
| `make render` / `make lab-up` | render Talos configs, or build the cluster (Chapter 4) from `infra/talos/cluster.env` |
| `make images` / `make bundle` | refresh the Zarf image lists, then build the signed transfer package (Chapter 3) |

Copy `.env.example` to `.env` for the variables the workstation scripts read. Never commit `.env`.

## Versions

[`VERSIONS.md`](VERSIONS.md) lists every pinned version and the date it was checked. Pins change in one pull request across `VERSIONS.md`, the file that uses them, and `airgap/zarf.yaml`.

## Licence

The code is MIT-licensed (see `LICENSE`). The book text is © 2026 Prosperity Paul Kipingili, all rights reserved. You are welcome to use the code in your own platforms; attribution ("Sovereign AI Stacks by Prosperity Paul Kipingili") is appreciated but not required.

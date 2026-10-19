# =============================================================================
# Makefile: one entry point for the checks and builds in this repository
# Book: Sovereign AI Stacks
#
#   make lint      ruff, shellcheck, yamllint on everything
#   make test      every pytest suite, each from its own directory
#   make check     lint + test (what CI runs)
#   make render    render Talos machine configs from infra/talos/cluster.env
#   make lab-up    build the cluster (Chapter 4); needs cluster.env and nodes
#   make images    refresh the Zarf images lists from charts and manifests
#   make bundle    build the signed Zarf package into transfer/ (Chapter 3)
# =============================================================================
SHELL := bash
.SHELLFLAGS := -Eeuo pipefail -c
.DEFAULT_GOAL := help

PYTHON ?= python3
CLUSTER_ENV ?= infra/talos/cluster.env
TRANSFER ?= transfer
# Zarf splits the package into chunks of this many MB (fits FAT32/exFAT media).
CHUNK_MB ?= 3900

PY_SUITES := tools/sizing tools/bench platform/inference apps/gateway_admin \
             apps/rag apps/audit platform/observability/tools evals \
             ops/scripts ops/backup/qdrant

# Files that are templates before envsubst, so not valid YAML on their own.
YAML_TEMPLATES := infra/talos/patches/common.yaml

.PHONY: help lint test check render lab-up images bundle

help:
	@sed -n '4,11p' Makefile | sed 's/^# \{0,1\}//'

lint:
	ruff check .
	git ls-files -co --exclude-standard '*.sh' | xargs -r shellcheck
	git ls-files -co --exclude-standard '*.yaml' '*.yml' \
	  | grep -vxF -f <(printf '%s\n' $(YAML_TEMPLATES)) \
	  | xargs -r yamllint -d relaxed --no-warnings

test:
	@for d in $(PY_SUITES); do \
	  echo "== $$d"; (cd "$$d" && $(PYTHON) -m pytest -p no:cacheprovider); \
	done

check: lint test

render:
	infra/talos/bootstrap.sh $(CLUSTER_ENV) render

lab-up:
	infra/talos/bootstrap.sh $(CLUSTER_ENV)

images:
	zarf dev find-images airgap/ --update

bundle:
	@[ -n "$${COSIGN_PASSWORD:-}" ] || { echo "set COSIGN_PASSWORD for cosign.key" >&2; exit 1; }
	mkdir -p $(TRANSFER)
	zarf package create airgap/ --confirm -o $(TRANSFER)/ -m $(CHUNK_MB) \
	  --signing-key cosign.key --signing-key-pass "$$COSIGN_PASSWORD"

#!/usr/bin/env bash
# =============================================================================
# test-rules.sh — lint and unit-test the platform's Prometheus rules offline
# Book: Sovereign AI Stacks, Chapter 9   Requires: promtool 3.15.0, Python 3.13
#
# promtool understands plain rule files, not PrometheusRule objects, so the
# script extracts spec.groups into a temporary directory next to a copy of
# the test file, then runs promtool check + test there.
# Usage: test-rules.sh [RULES_FILE [TEST_FILE]]
# =============================================================================
set -Eeuo pipefail

usage() {
  echo "usage: $0 [RULES_FILE [TEST_FILE]]" >&2
  echo "  defaults: alerts/llm-platform-rules.yaml and alerts/tests/llm-platform-rules.test.yaml" >&2
  exit 2
}

[[ $# -le 2 ]] || usage
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rules="${1:-$here/llm-platform-rules.yaml}"
tests="${2:-$here/tests/llm-platform-rules.test.yaml}"
lint="$here/../tools/obs_lint.py"

for f in "$rules" "$tests" "$lint"; do
  [[ -f "$f" ]] || { echo "not found: $f" >&2; exit 2; }
done
command -v promtool >/dev/null || { echo "promtool not found in PATH" >&2; exit 2; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

python3 "$lint" rules "$rules"
python3 "$lint" extract-rules "$rules" >"$work/llm-platform-rules.prom.yaml"
cp "$tests" "$work/rules.test.yaml"

promtool check rules "$work/llm-platform-rules.prom.yaml"
promtool test rules "$work/rules.test.yaml"

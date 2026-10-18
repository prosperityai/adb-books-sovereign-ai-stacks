#!/usr/bin/env bash
# =============================================================================
# install_harbor.sh — install Harbor 2.15.4 offline and create the projects
#                     and robot accounts the platform uses
#
# Book:    Sovereign AI Stacks, Chapter 3
# Runs on: the enclave registry VM (Ubuntu 24.04, Docker Engine + compose plugin)
#
# Usage:
#   export HARBOR_HOSTNAME=harbor.enclave.internal
#   export HARBOR_TLS_CERT=/etc/harbor/tls/harbor.crt
#   export HARBOR_TLS_KEY=/etc/harbor/tls/harbor.key
#   export HARBOR_ADMIN_PASSWORD=...  HARBOR_DB_PASSWORD=...
#   export HARBOR_DATA_VOLUME=/srv/harbor
#   sudo -E ./install_harbor.sh /import/harbor-offline-installer-v2.15.4.tgz
#
# Writes robot credentials to ./robot-credentials.json (mode 600). Move them
# into your secret store immediately; Chapter 7 loads them into Kubernetes.
# =============================================================================
set -Eeuo pipefail

INSTALLER=${1:?usage: install_harbor.sh <harbor-offline-installer.tgz>}
HERE=$(cd "$(dirname "$0")" && pwd)

for v in HARBOR_HOSTNAME HARBOR_TLS_CERT HARBOR_TLS_KEY HARBOR_ADMIN_PASSWORD HARBOR_DB_PASSWORD HARBOR_DATA_VOLUME; do
  [ -n "${!v:-}" ] || { echo "missing environment variable $v" >&2; exit 1; }
done
for bin in docker envsubst curl python3; do
  command -v "$bin" >/dev/null || { echo "$bin not found" >&2; exit 1; }
done

# 1. Unpack and render the configuration. envsubst is given an explicit
#    variable list so a literal '$' in a password is never expanded.
WORK=/opt/harbor
mkdir -p "$WORK" "$HARBOR_DATA_VOLUME"
tar -xzf "$INSTALLER" -C /opt
# shellcheck disable=SC2016 # a literal variable list for envsubst, not an expansion
envsubst '${HARBOR_HOSTNAME} ${HARBOR_TLS_CERT} ${HARBOR_TLS_KEY} ${HARBOR_ADMIN_PASSWORD} ${HARBOR_DB_PASSWORD} ${HARBOR_DATA_VOLUME}' \
  < "$HERE/harbor.yml.tmpl" > "$WORK/harbor.yml"
chmod 600 "$WORK/harbor.yml"

# 2. Load images from the offline tarball and install with the Trivy scanner.
( cd "$WORK" && ./install.sh --with-trivy )

# 3. Wait for the API.
API="https://$HARBOR_HOSTNAME/api/v2.0"
AUTH=(-u "admin:$HARBOR_ADMIN_PASSWORD")
for _ in $(seq 1 60); do
  curl -fsS "${AUTH[@]}" "$API/health" >/dev/null 2>&1 && break
  sleep 5
done
curl -fsS "${AUTH[@]}" "$API/health" >/dev/null

# 4. Projects. All private: nothing in the enclave is anonymous.
#    mirror  - upstream images, paths preserved (mirror/cilium/cilium, ...)
#    models  - model weights as OCI artifacts
#    charts  - Helm charts as OCI artifacts
#    apps    - images we build ourselves (RAG API, audit sink)
for project in mirror models charts apps; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' \
    -X POST "$API/projects" -d "{\"project_name\":\"$project\",\"public\":false}")
  case "$code" in
    201) echo "created project $project" ;;
    409) echo "project $project already exists" ;;
    *)   echo "creating project $project failed: HTTP $code" >&2; exit 1 ;;
  esac
done

# 5. Robot accounts: one that can push (import station only) and one that can
#    only pull (every cluster node). Separate credentials, separate blast radius.
robot() {
  local name=$1 actions=$2
  curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' -X POST "$API/robots" -d @- <<JSON
{"name":"$name","description":"Sovereign AI Stacks $name","duration":-1,"level":"system",
 "permissions":[{"kind":"project","namespace":"*","access":[$actions]}]}
JSON
}
PULL='{"resource":"repository","action":"pull"}'
PUSH='{"resource":"repository","action":"pull"},{"resource":"repository","action":"push"},{"resource":"artifact","action":"read"},{"resource":"tag","action":"create"}'

umask 077
{
  echo '{'
  printf '"push": %s,\n' "$(robot import-push "$PUSH")"
  printf '"pull": %s\n' "$(robot cluster-pull "$PULL")"
  echo '}'
} > robot-credentials.json
python3 -m json.tool robot-credentials.json >/dev/null
echo "robot credentials written to $(pwd)/robot-credentials.json"

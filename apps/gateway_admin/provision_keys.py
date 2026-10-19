#!/usr/bin/env python3
r"""provision_keys.py — make the gateway's teams and keys match tenants.yaml.

Book: Sovereign AI Stacks, Chapter 6
Requires: Python 3.13, PyYAML 6.0 (talks to the LiteLLM 1.106 management API)

Idempotent: existing teams are updated to the file's limits; existing keys are
left alone (their secret is never shown twice). Each NEW key is written as a
Kubernetes Secret manifest in --out, ready for `kubectl apply`, and never
printed to the terminal.

    export LITELLM_MASTER_KEY=$(kubectl -n ai-gateway get secret litellm-master-key \
        -o jsonpath='{.data.masterkey}' | base64 -d)
    python provision_keys.py --gateway http://localhost:4000 --tenants tenants.yaml --out ./new-secrets
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

KEY_FIELDS = ("rpm_limit", "tpm_limit", "max_parallel_requests")


class Gateway:
    """Thin client for the LiteLLM management endpoints this script needs."""

    def __init__(self, base_url: str, master_key: str) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict | list:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]
            raise SystemExit(f"{method} {path} failed: HTTP {err.code}: {detail}") from None

    def teams(self) -> dict[str, dict]:
        """team_alias -> team object."""
        result = self._call("GET", "/team/list")
        return {t["team_alias"]: t for t in result if t.get("team_alias")}

    def create_team(self, body: dict) -> dict:
        return self._call("POST", "/team/new", body)

    def update_team(self, body: dict) -> dict:
        return self._call("POST", "/team/update", body)

    def key_exists(self, alias: str) -> bool:
        result = self._call("GET", "/key/list", query={"key_alias": alias, "size": 1})
        return int(result.get("total_count", len(result.get("keys", [])))) > 0

    def generate_key(self, body: dict) -> dict:
        return self._call("POST", "/key/generate", body)


def team_models(tenant: dict) -> list[str]:
    """The tenant's models plus any key-only alias (e.g. the -batch model).

    LiteLLM checks the TEAM's model list on every request, as well as the
    key's, so a key-only alias must also be on the team or its calls fail.
    """
    extra = {m for key in tenant.get("keys", []) for m in key.get("models", [])} - set(tenant["models"])
    return list(tenant["models"]) + sorted(extra)


def team_body(tenant: dict) -> dict:
    return {
        "team_alias": tenant["id"],
        "models": team_models(tenant),
        "rpm_limit": tenant["rpm_limit"],
        "tpm_limit": tenant["tpm_limit"],
        "metadata": {"tenant_name": tenant["name"], "managed_by": "provision_keys.py"},
    }


def key_body(tenant: dict, team_id: str, key: dict) -> dict:
    body = {
        "key_alias": key["alias"],
        "team_id": team_id,
        "models": key.get("models", tenant["models"]),
        "metadata": {"tenant": tenant["id"], "purpose": key["purpose"], "managed_by": "provision_keys.py"},
    }
    for field in KEY_FIELDS:
        if field in key:
            body[field] = key[field]
    unknown = set(body["models"]) - set(tenant["models"]) - {f"{m}-batch" for m in tenant["models"]}
    if unknown:
        raise SystemExit(f"key {key['alias']} requests models outside its tenant: {sorted(unknown)}")
    return body


def secret_manifest(name: str, namespace: str, value: str, tenant: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{name}-gateway-key",
            "namespace": namespace,
            "labels": {"sovereign-ai/tenant": tenant, "sovereign-ai/managed-by": "provision_keys"},
        },
        "type": "Opaque",
        "data": {"key": base64.b64encode(value.encode()).decode()},
    }


def provision(gw: Gateway, tenants: list[dict], out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    counts = {"teams_created": 0, "teams_updated": 0, "keys_created": 0, "keys_existing": 0}
    existing = gw.teams()
    for tenant in tenants:
        body = team_body(tenant)
        if tenant["id"] in existing:
            team_id = existing[tenant["id"]]["team_id"]
            gw.update_team({"team_id": team_id, **body})
            counts["teams_updated"] += 1
        else:
            team_id = gw.create_team(body)["team_id"]
            counts["teams_created"] += 1
        for key in tenant.get("keys", []):
            if gw.key_exists(key["alias"]):
                counts["keys_existing"] += 1
                continue
            secret = gw.generate_key(key_body(tenant, team_id, key))["key"]
            manifest = secret_manifest(key["alias"], key["secret_namespace"], secret, tenant["id"])
            path = out / f"{key['alias']}.secret.yaml"
            path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
            path.chmod(0o600)
            counts["keys_created"] += 1
            print(f"created key {key['alias']} -> {path}")
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", required=True)
    p.add_argument("--tenants", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    master = os.environ.get("LITELLM_MASTER_KEY")
    if not master:
        print("set LITELLM_MASTER_KEY", file=sys.stderr)
        return 2
    tenants = yaml.safe_load(args.tenants.read_text(encoding="utf-8"))["tenants"]
    counts = provision(Gateway(args.gateway, master), tenants, args.out)
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

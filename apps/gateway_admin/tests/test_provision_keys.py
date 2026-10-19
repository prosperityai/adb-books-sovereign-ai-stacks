"""Tests for provision_keys.py against an in-process fake of the LiteLLM API."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from provision_keys import Gateway, key_body, provision  # noqa: E402

TENANTS = yaml.safe_load((Path(__file__).resolve().parent.parent / "tenants.yaml").read_text())["tenants"]


class FakeLiteLLM(BaseHTTPRequestHandler):
    teams: dict[str, dict] = {}
    keys: dict[str, dict] = {}
    calls: list[tuple[str, str, dict]] = []

    def log_message(self, *args: object) -> None:  # silence test output
        return

    def _reply(self, payload: object, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != "Bearer sk-master":
            return self._reply({"error": "unauthorized"}, 401)
        url = urlparse(self.path)
        if url.path == "/team/list":
            return self._reply(list(self.teams.values()))
        if url.path == "/key/list":
            alias = parse_qs(url.query)["key_alias"][0]
            found = [k for k in self.keys.values() if k["key_alias"] == alias]
            return self._reply({"keys": found, "total_count": len(found)})
        return self._reply({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(("POST", self.path, body))
        if self.path == "/team/new":
            team = {"team_id": f"team-{len(self.teams) + 1}", **body}
            self.teams[team["team_id"]] = team
            return self._reply(team)
        if self.path == "/team/update":
            self.teams[body["team_id"]].update(body)
            return self._reply(self.teams[body["team_id"]])
        if self.path == "/key/generate":
            key = {"key": f"sk-{body['key_alias']}", **body}
            self.keys[body["key_alias"]] = key
            return self._reply(key)
        return self._reply({"error": "not found"}, 404)


@pytest.fixture
def gateway() -> Gateway:
    FakeLiteLLM.teams, FakeLiteLLM.keys, FakeLiteLLM.calls = {}, {}, []
    server = HTTPServer(("127.0.0.1", 0), FakeLiteLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Gateway(f"http://127.0.0.1:{server.server_port}", "sk-master")
    server.shutdown()


def test_first_run_creates_everything(gateway: Gateway, tmp_path: Path) -> None:
    counts = provision(gateway, TENANTS, tmp_path)
    assert counts == {"teams_created": 3, "teams_updated": 0, "keys_created": 5, "keys_existing": 0}
    secret = yaml.safe_load((tmp_path / "chat-ministry-health.secret.yaml").read_text())
    assert secret["metadata"] == {
        "name": "chat-ministry-health-gateway-key",
        "namespace": "chat-ui",
        "labels": {"sovereign-ai/tenant": "ministry-health", "sovereign-ai/managed-by": "provision_keys"},
    }
    assert (tmp_path / "chat-ministry-health.secret.yaml").stat().st_mode & 0o777 == 0o600


def test_second_run_is_idempotent(gateway: Gateway, tmp_path: Path) -> None:
    provision(gateway, TENANTS, tmp_path / "a")
    counts = provision(gateway, TENANTS, tmp_path / "b")
    assert counts == {"teams_created": 0, "teams_updated": 3, "keys_created": 0, "keys_existing": 5}
    assert list((tmp_path / "b").iterdir()) == []


def test_batch_key_limits_are_sent(gateway: Gateway, tmp_path: Path) -> None:
    provision(gateway, TENANTS, tmp_path)
    batch = FakeLiteLLM.keys["ministry-health-claims-batch"]
    assert batch["models"] == ["qwen3.8-27b-batch"]
    assert batch["rpm_limit"] == 30 and batch["max_parallel_requests"] == 4
    # The gateway also checks the team's models, so the batch alias must be there too.
    health = next(t for t in FakeLiteLLM.teams.values() if t["team_alias"] == "ministry-health")
    assert health["models"] == ["qwen3.8-27b", "gpt-oss-120b", "bge-m3", "qwen3.8-27b-batch"]


def test_key_cannot_escape_its_tenant() -> None:
    tenant = {"id": "t", "models": ["qwen3.8-27b"]}
    with pytest.raises(SystemExit, match="outside its tenant"):
        key_body(tenant, "team-1", {"alias": "x", "purpose": "p", "models": ["gpt-oss-120b"]})


def test_bad_master_key_fails_loudly(tmp_path: Path, gateway: Gateway) -> None:
    gateway.headers["Authorization"] = "Bearer wrong"
    with pytest.raises(SystemExit, match="HTTP 401"):
        provision(gateway, TENANTS, tmp_path)

"""The HTTP contract: authentication, idempotency, failure modes."""

from __future__ import annotations

import json

import pytest
from audit.sink import Settings, create_app
from audit.store import MemoryRepository
from conftest import AUTH, TOKEN, event
from fastapi.testclient import TestClient


def post(client: TestClient, body: object, headers: dict | None = None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post("/v1/events", content=data, headers=headers if headers is not None else AUTH)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-wrong-token-wrong-token-wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer "},
    ],
)
def test_rejects_missing_or_wrong_token(client: TestClient, repo: MemoryRepository, headers: dict) -> None:
    r = post(client, [event()], headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert repo.rows == {}


def test_refuses_to_start_without_a_token(settings: Settings) -> None:
    for weak in ("", "short-token"):
        with pytest.raises(RuntimeError, match="refusing to start"):
            create_app(Settings(weak, settings.retention, settings.trusted_user_keys), MemoryRepository())


def test_from_env_reads_token_file(tmp_path) -> None:
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    s = Settings.from_env({"AUDIT_SINK_TOKEN_FILE": str(f), "AUDIT_DB_PASSWORD": "pw"})
    assert s.token == TOKEN
    assert "user=audit_writer" in s.db_conninfo and "sslmode=verify-full" in s.db_conninfo


def test_accepts_litellm_batch(client: TestClient, repo: MemoryRepository) -> None:
    r = post(client, [event(), event(litellm_call_id="call-2")])
    assert r.status_code == 200
    assert r.json() == {"received": 2, "inserted": 2, "duplicates": 0, "rejected": 0}
    stored = repo.rows["call-2"]
    assert "19850423" not in json.dumps(stored.messages)


def test_accepts_single_object(client: TestClient, repo: MemoryRepository) -> None:
    assert post(client, event()).json()["inserted"] == 1


def test_resent_batch_is_idempotent(client: TestClient, repo: MemoryRepository) -> None:
    batch = [event(litellm_call_id=f"call-{i}") for i in range(5)]
    assert post(client, batch).json()["inserted"] == 5
    again = post(client, batch).json()
    assert again == {"received": 5, "inserted": 0, "duplicates": 5, "rejected": 0}
    assert len(repo.rows) == 5


def test_bad_event_does_not_sink_the_batch(client: TestClient, repo: MemoryRepository) -> None:
    r = post(client, [event(), event(litellm_call_id="no-model", model=None, model_group=None)])
    assert r.json() == {"received": 2, "inserted": 1, "duplicates": 0, "rejected": 1}


def test_invalid_json_is_400(client: TestClient) -> None:
    assert post(client, b"{not json").status_code == 400


def test_body_limit(client: TestClient, settings: Settings) -> None:
    big = [event(litellm_call_id=f"c{i}", messages=[{"role": "user", "content": "x" * 4096}]) for i in range(80)]
    assert post(client, big).status_code == 413


def test_store_outage_is_503_so_the_sender_can_retry(client: TestClient, repo: MemoryRepository) -> None:
    repo.fail = True
    assert post(client, [event()]).status_code == 503
    assert client.get("/readyz").status_code == 503
    repo.fail = False
    assert post(client, [event()]).json()["inserted"] == 1
    assert client.get("/readyz").status_code == 200


def test_metrics_count_without_content(client: TestClient) -> None:
    post(client, [event()])
    post(client, [event()], {})
    text = client.get("/metrics").text
    assert "audit_sink_records_inserted_total 1" in text
    assert 'audit_sink_redactions_applied_total{type="NIDA"} 1' in text
    assert 'audit_sink_requests_total{code="401"} 1' in text
    assert "amina" not in text and "19850423" not in text


def test_no_openapi_surface(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404

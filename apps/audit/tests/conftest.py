"""Shared fixtures: realistic LiteLLM payloads and an app wired to memory storage."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from audit.models import RetentionPolicy
from audit.sink import Settings, create_app
from audit.store import MemoryRepository
from fastapi.testclient import TestClient

TOKEN = "t" * 48
NOW = datetime(2026, 10, 19, 9, 30, tzinfo=UTC)

# Shape of a LiteLLM 1.106 StandardLoggingPayload as the generic_api callback
# sends it (fields this sink reads, plus some it ignores).
BASE_EVENT: dict = {
    "id": "chatcmpl-8f1c2e",
    "litellm_call_id": "6b0f6a3e-2f5e-4f51-9d61-0c0e1a8c9b01",
    "trace_id": "6b0f6a3e-2f5e-4f51-9d61-0c0e1a8c9b01",
    "call_type": "acompletion",
    "stream": False,
    "status": "success",
    "response_cost": 0.0,
    "model": "qwen3.8-27b",
    "model_group": "qwen3.8-27b",
    "api_base": "http://vllm-qwen3-8-27b.inference.svc:8000/v1",
    "startTime": 1792402200.125,
    "endTime": 1792402202.5,
    "prompt_tokens": 41,
    "completion_tokens": 88,
    "total_tokens": 129,
    "end_user": "amina.juma@afya.go.tz",
    "requester_ip_address": "10.244.3.17",
    "metadata": {
        "user_api_key_hash": "88dc28d0f030c55ed4ab77ed8faf098196cb1c05df778539800c9f1243fe6b4b",
        "user_api_key_alias": "chat-ministry-health",
        "user_api_key_team_id": "team-mh-01",
        "user_api_key_team_alias": "ministry-health",
        "user_api_key_end_user_id": "amina.juma@afya.go.tz",
        "requester_ip_address": "10.244.3.17",
    },
    "messages": [
        {"role": "system", "content": "Wewe ni msaidizi wa Wizara ya Afya."},
        {
            "role": "user",
            "content": "Mgonjwa mwenye NIDA 19850423-11101-00001-25, simu +255 754 123 456, "
            "anahitaji rufaa. Andika barua kwa daktari@kcmc.or.tz.",
        },
    ],
    "response": {
        "id": "chatcmpl-8f1c2e",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Barua ya rufaa kwa mgonjwa (simu 0754123456): ..."},
            }
        ],
    },
    "error_str": None,
}


def event(**overrides: object) -> dict:
    ev = copy.deepcopy(BASE_EVENT)
    for k, v in overrides.items():
        if k == "metadata":
            ev["metadata"].update(v)  # type: ignore[arg-type]
        else:
            ev[k] = v
    return ev


@pytest.fixture
def settings() -> Settings:
    return Settings(
        token=TOKEN,
        retention=RetentionPolicy.from_json(
            '{"*": {"metadata": 730, "content": 180},'
            ' "ministry-health": {"metadata": 2555, "content": 365},'
            ' "revenue-authority": {"metadata": 3650, "content": 365}}'
        ),
        trusted_user_keys=frozenset({"chat-ministry-health", "chat-revenue-authority"}),
        max_body_bytes=256 * 1024,
    )


@pytest.fixture
def repo() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def client(settings: Settings, repo: MemoryRepository) -> TestClient:
    return TestClient(create_app(settings, repo, clock=lambda: NOW))


AUTH = {"Authorization": f"Bearer {TOKEN}"}

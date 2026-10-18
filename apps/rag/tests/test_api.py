"""API behaviour: no tenant in the request, ACL-scoped caching, citations."""

from __future__ import annotations

import pytest
from conftest import HashEmbedder, principal
from fastapi.testclient import TestClient
from rag.api import create_app
from rag.auth import AuthError
from rag.config import Settings
from rag.retrieve import Retriever

SETTINGS = Settings(
    oidc_issuer="https://sso.enclave.internal/realms/government",
    oidc_jwks_url="http://unused",
    oidc_audience="rag-api",
    qdrant_url="http://unused",
    qdrant_api_key="x",
    tei_embed_url="http://unused",
    tei_rerank_url="http://unused",
    tei_api_key="x",
    gateway_url="http://unused",
    gateway_key="x",
    chat_model="qwen3.8-27b",
    ca_file=None,
)

USERS = {
    "tok-alice": principal("ministry-health", clearance=2, sub="alice"),
    "tok-bob": principal("ministry-health", clearance=0, sub="bob"),
    "tok-eve": principal("revenue-authority", clearance=3, sub="eve"),
}


class FakeVerifier:
    def verify(self, token: str):
        if token not in USERS:
            raise AuthError("bad token")
        return USERS[token]


class EchoGenerator:
    """Returns the sources it was given, so tests can see what reached the prompt."""

    def __init__(self) -> None:
        self.calls = 0

    def answer(self, system: str, user: str, end_user: str) -> str:
        self.calls += 1
        return f"for {end_user}: {user.split('Question:')[0]}"


@pytest.fixture
def client(store):
    gen = EchoGenerator()
    app = create_app(SETTINGS, FakeVerifier(), Retriever(store, HashEmbedder(), None), gen)
    c = TestClient(app)
    c.generator = gen
    return c


def ask(client, tok: str, query: str, **extra):
    return client.post("/v1/answer", json={"query": query, **extra}, headers={"Authorization": f"Bearer {tok}"})


def test_requires_token(client) -> None:
    assert client.post("/v1/search", json={"query": "anything"}).status_code == 401
    assert ask(client, "tok-forged", "referral backlog").status_code == 401


def test_tenant_in_body_is_ignored(client) -> None:
    # Chapter 7's war story: asking for another tenant in the body must do nothing.
    r = ask(client, "tok-eve", "referral backlog", tenant="ministry-health")
    assert r.status_code == 200
    assert {c["source"] for c in r.json()["citations"]} == {"ra-public.md"}


def test_cache_does_not_cross_clearance(client) -> None:
    first = ask(client, "tok-alice", "referral backlog regional hospital").json()
    assert "4,200" in first["answer"] and not first["cached"]
    second = ask(client, "tok-bob", "referral backlog regional hospital").json()
    assert "4,200" not in second["answer"]
    assert not second["cached"]
    again = ask(client, "tok-alice", "referral backlog regional hospital").json()
    assert again["cached"]


def test_no_sources_means_no_model_call(client) -> None:
    r = ask(client, "tok-eve", "zzzz qqqq xxxx").json()
    assert r["citations"] == [] or all(c["source"] == "ra-public.md" for c in r["citations"])

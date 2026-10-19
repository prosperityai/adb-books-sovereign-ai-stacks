"""Retries, back-pressure and token refresh against a scripted fake server.

Book: Sovereign AI Stacks, Chapter 11
"""

from __future__ import annotations

import asyncio

import httpx
from evalkit.client import Endpoints

OK = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 1}}


def _scripted(statuses: list[int], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = statuses.pop(0) if statuses else 200
        return httpx.Response(status, json=OK if status == 200 else {"error": {"message": "x"}})

    return httpx.MockTransport(handler)


def _chat(ep: Endpoints):
    async def go():
        try:
            return await ep.chat("qwen3.8-27b", [{"role": "user", "content": "hi"}], 8, {})
        finally:
            await ep.aclose()

    return asyncio.run(go())


def test_429_is_retried_then_succeeds():
    seen: list[httpx.Request] = []
    ep = Endpoints("http://gw", "sk-k", transport=_scripted([429, 503], seen), backoff_s=0)
    reply = _chat(ep)
    assert reply.error is None and reply.text == "ok" and reply.retries == 2 and len(seen) == 3
    assert reply.prompt_tokens == 7


def test_401_is_not_retried():
    seen: list[httpx.Request] = []
    ep = Endpoints("http://gw", "sk-bad", transport=_scripted([401], seen), backoff_s=0)
    reply = _chat(ep)
    assert reply.error.startswith("HTTP 401") and len(seen) == 1


def test_retries_exhausted_is_an_error():
    seen: list[httpx.Request] = []
    ep = Endpoints("http://gw", "sk-k", transport=_scripted([429] * 10, seen), max_retries=2, backoff_s=0)
    reply = _chat(ep)
    assert reply.error == "HTTP 429" and len(seen) == 3


def test_request_is_deterministic_and_tagged():
    seen: list[httpx.Request] = []
    _chat(Endpoints("http://gw", "sk-k", transport=_scripted([], seen)))
    body = seen[0].read().decode()
    assert '"temperature":0' in body.replace(" ", "") and '"seed":1234' in body.replace(" ", "")
    assert seen[0].headers["authorization"] == "Bearer sk-k"


def test_expired_rag_token_is_refreshed_once():
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer old":
            return httpx.Response(401, json={"detail": "invalid token"})
        return httpx.Response(200, json={"answer": "a", "citations": []})

    async def refresh() -> str:
        return "new"

    async def go():
        ep = Endpoints("http://gw", "sk", "http://rag", "old", refresh, transport=httpx.MockTransport(handler))
        try:
            return await ep.rag_answer("q?", {})
        finally:
            await ep.aclose()

    reply = asyncio.run(go())
    assert reply.error is None and tokens == ["Bearer old", "Bearer new"]


def test_cached_rag_answers_are_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "a", "citations": [], "cached": True})

    async def go():
        ep = Endpoints("http://gw", "sk", "http://rag", "tok", transport=httpx.MockTransport(handler))
        try:
            return await ep.rag_answer("q?", {})
        finally:
            await ep.aclose()

    assert asyncio.run(go()).cached is True

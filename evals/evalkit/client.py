"""HTTP clients: the gateway's OpenAI-compatible API and the RAG API.

Book: Sovereign AI Stacks, Chapter 11

Every call is timed and its token usage recorded. Retries cover the gateway's
own back-pressure (429 when the eval-harness key exceeds max_parallel_requests)
and transient upstream errors; a 4xx other than 429 is a harness bug and is
never retried.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

RETRY_STATUS = frozenset({429, 502, 503, 504})


@dataclass
class Reply:
    text: str = ""
    citations: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    retries: int = 0
    error: str | None = None
    cached: bool = False


class Endpoints:
    def __init__(
        self,
        gateway_url: str,
        gateway_key: str,
        rag_url: str | None = None,
        rag_token: str | None = None,
        rag_token_refresh: Callable[[], Awaitable[str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        backoff_s: float = 1.0,
    ) -> None:
        self.chat_url = gateway_url.rstrip("/") + "/v1/chat/completions"
        self.rag_url = rag_url.rstrip("/") + "/v1/answer" if rag_url else None
        self.gateway_key = gateway_key
        self.rag_token = rag_token
        self.rag_token_refresh = rag_token_refresh
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.http = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _post(self, url: str, token: str, body: dict) -> tuple[httpx.Response | None, float, int, str | None]:
        retries, last_error = 0, None
        for attempt in range(self.max_retries + 1):
            start = time.perf_counter()
            try:
                resp = await self.http.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as err:
                last_error = f"{type(err).__name__}: {err}"
            else:
                elapsed = time.perf_counter() - start
                if resp.status_code not in RETRY_STATUS:
                    return resp, elapsed, retries, None
                last_error = f"HTTP {resp.status_code}"
            if attempt < self.max_retries:
                retries += 1
                await asyncio.sleep(self.backoff_s * 2**attempt)
        return None, 0.0, retries, last_error

    async def chat(self, model: str, messages: list[dict], max_tokens: int, metadata: dict) -> Reply:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "seed": 1234,
            "max_tokens": max_tokens,
            "user": "eval-harness",
            # LiteLLM keeps "metadata" for its own logging callbacks (Langfuse tags,
            # Chapter 9) and does not forward it to the model server.
            "metadata": metadata,
        }
        resp, latency, retries, error = await self._post(self.chat_url, self.gateway_key, body)
        if resp is None:
            return Reply(retries=retries, error=error)
        if resp.status_code != 200:
            return Reply(latency_s=latency, retries=retries, error=f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage") or {}
        return Reply(
            text=data["choices"][0]["message"].get("content") or "",
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_s=latency,
            retries=retries,
        )

    async def rag_answer(self, query: str, extra_body: dict, top_k: int = 6) -> Reply:
        if not self.rag_url or not self.rag_token:
            return Reply(error="RAG target not configured (--rag-url and a RAG token are required)")
        body = {**extra_body, "query": query, "top_k": top_k}
        resp, latency, retries, error = await self._post(self.rag_url, self.rag_token, body)
        if resp is not None and resp.status_code == 401 and self.rag_token_refresh:
            # Keycloak access tokens live minutes; a long run outlives the first one.
            self.rag_token = await self.rag_token_refresh()
            resp, latency, more, error = await self._post(self.rag_url, self.rag_token, body)
            retries += more + 1
        if resp is None:
            return Reply(retries=retries, error=error)
        if resp.status_code != 200:
            return Reply(latency_s=latency, retries=retries, error=f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        # The RAG API does not report token usage; its generation is metered at the gateway
        # under the rag-api key, so cost for RAG runs comes from the gateway's spend logs.
        return Reply(
            text=data.get("answer", ""),
            citations=data.get("citations", []),
            latency_s=latency,
            retries=retries,
            # The RAG API caches answers for RAG_CACHE_TTL seconds (Chapter 8). A cached
            # answer measures yesterday's configuration and a near-zero latency.
            cached=bool(data.get("cached", False)),
        )


async def client_credentials_token(
    token_url: str, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Fetch a service-account access token from Keycloak (Chapter 7's rag-<tenant> clients)."""
    async with httpx.AsyncClient(timeout=30, transport=transport) as http:
        resp = await http.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

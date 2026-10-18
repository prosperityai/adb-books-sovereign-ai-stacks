"""HTTP clients for the in-enclave model services (TEI and the AI gateway)."""

from __future__ import annotations

import httpx


class Embedder:
    """Text Embeddings Inference (TEI) /embed for BAAI/bge-m3 (1024 dimensions)."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=60, headers={"Authorization": f"Bearer {api_key}"})

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self.http.post(f"{self.base}/embed", json={"inputs": texts, "normalize": True, "truncate": True})
        resp.raise_for_status()
        return resp.json()


class Reranker:
    """TEI /rerank for BAAI/bge-reranker-v2-m3. Returns indices ordered by relevance."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=60, headers={"Authorization": f"Bearer {api_key}"})

    def rerank(self, query: str, texts: list[str]) -> list[tuple[int, float]]:
        if not texts:
            return []
        resp = self.http.post(f"{self.base}/rerank", json={"query": query, "texts": texts, "truncate": True})
        resp.raise_for_status()
        return [(int(r["index"]), float(r["score"])) for r in resp.json()]


class Generator:
    """Chat completions through the AI gateway (Chapter 6), never a model directly."""

    def __init__(self, gateway_url: str, key: str, model: str, client: httpx.Client | None = None) -> None:
        self.url = gateway_url.rstrip("/") + "/v1/chat/completions"
        self.model = model
        self.http = client or httpx.Client(timeout=300, headers={"Authorization": f"Bearer {key}"})

    def answer(self, system: str, user: str, end_user: str) -> str:
        resp = self.http.post(
            self.url,
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1,
                "max_tokens": 900,
                "user": end_user,  # recorded by the gateway for spend, limits and audit
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

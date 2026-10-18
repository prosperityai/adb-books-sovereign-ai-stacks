"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Identity (Chapter 7)
    oidc_issuer: str
    oidc_jwks_url: str
    oidc_audience: str
    # In-enclave services
    qdrant_url: str
    qdrant_api_key: str
    tei_embed_url: str
    tei_rerank_url: str
    tei_api_key: str
    gateway_url: str
    gateway_key: str
    chat_model: str
    ca_file: str | None
    # Retrieval behaviour
    candidates: int = 40
    top_k: int = 6
    cache_ttl_seconds: int = 300

    @staticmethod
    def from_env() -> Settings:
        e = os.environ
        return Settings(
            oidc_issuer=e["OIDC_ISSUER"],
            oidc_jwks_url=e.get("OIDC_JWKS_URL", e["OIDC_ISSUER"].rstrip("/") + "/protocol/openid-connect/certs"),
            oidc_audience=e.get("OIDC_AUDIENCE", "rag-api"),
            qdrant_url=e.get("QDRANT_URL", "http://qdrant.rag.svc:6333"),
            qdrant_api_key=e["QDRANT_API_KEY"],
            tei_embed_url=e.get("TEI_EMBED_URL", "http://tei-bge-m3.rag.svc:8080"),
            tei_rerank_url=e.get("TEI_RERANK_URL", "http://tei-reranker.rag.svc:8080"),
            tei_api_key=e["TEI_API_KEY"],
            gateway_url=e.get("GATEWAY_URL", "http://litellm.ai-gateway.svc:4000"),
            gateway_key=e["GATEWAY_KEY"],
            chat_model=e.get("CHAT_MODEL", "qwen3.8-27b"),
            ca_file=e.get("SSL_CERT_FILE"),
            candidates=int(e.get("RAG_CANDIDATES", "40")),
            top_k=int(e.get("RAG_TOP_K", "6")),
            cache_ttl_seconds=int(e.get("RAG_CACHE_TTL", "300")),
        )

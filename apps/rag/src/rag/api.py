"""FastAPI service: /v1/search and /v1/answer, tenant and ACL from the token only.

Book: Sovereign AI Stacks, Chapter 8
Run: uvicorn rag.api:create_app --factory --host 0.0.0.0 --port 8000
"""

# No `from __future__ import annotations` here: FastAPI must resolve the
# Depends() inside Annotated[...] at definition time, in this closure.
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from rag.auth import AuthError, Principal, TokenVerifier
from rag.clients import Embedder, Generator, Reranker
from rag.config import Settings
from rag.retrieve import AnswerCache, Passage, Retriever

log = logging.getLogger("rag.api")

SYSTEM_PROMPT = (
    "You answer questions for government staff using ONLY the numbered sources provided. "
    "Cite sources as [1], [2]. If the sources do not contain the answer, say you cannot find it "
    "in the documents available to this user. Answer in the language of the question."
)


class Query(BaseModel):
    # Deliberately no "tenant" field: the tenant comes from the token.
    query: str = Field(min_length=3, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)


class PassageOut(BaseModel):
    text: str
    source: str
    heading: str
    doc_id: str
    score: float


class Answer(BaseModel):
    answer: str
    citations: list[PassageOut]
    cached: bool = False


def create_app(
    settings: Settings | None = None,
    verifier: TokenVerifier | None = None,
    retriever: Retriever | None = None,
    generator: Generator | None = None,
) -> FastAPI:
    s = settings or Settings.from_env()
    verifier = verifier or TokenVerifier(s.oidc_issuer, s.oidc_jwks_url, s.oidc_audience, s.ca_file)
    retriever = retriever or Retriever(
        QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key),
        Embedder(s.tei_embed_url, s.tei_api_key),
        Reranker(s.tei_rerank_url, s.tei_api_key),
    )
    generator = generator or Generator(s.gateway_url, s.gateway_key, s.chat_model)
    cache = AnswerCache(s.cache_ttl_seconds)
    app = FastAPI(title="Sovereign RAG", version="1.0.0", docs_url=None, redoc_url=None)

    def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            return verifier.verify(authorization.split(" ", 1)[1])
        except AuthError as err:
            log.warning("rejected token: %s", err)
            raise HTTPException(401, "invalid token") from None

    def to_out(passages: list[Passage]) -> list[PassageOut]:
        return [PassageOut(**p.__dict__) for p in passages]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/v1/search", response_model=list[PassageOut])
    def search(q: Query, p: Annotated[Principal, Depends(principal)]) -> list[PassageOut]:
        return to_out(retriever.search(p, q.query, s.candidates, q.top_k))

    @app.post("/v1/answer", response_model=Answer)
    def answer(q: Query, p: Annotated[Principal, Depends(principal)]) -> Answer:
        cached = cache.get(p, q.query)
        if cached is not None:
            return cached.model_copy(update={"cached": True})
        passages = retriever.search(p, q.query, s.candidates, q.top_k)
        if not passages:
            result = Answer(answer="I cannot find this in the documents available to you.", citations=[])
        else:
            context = "\n\n".join(
                f"[{i}] {ps.source} — {ps.heading}\n{ps.text}" for i, ps in enumerate(passages, start=1)
            )
            text = generator.answer(SYSTEM_PROMPT, f"Sources:\n{context}\n\nQuestion: {q.query}", end_user=p.subject)
            result = Answer(answer=text, citations=to_out(passages))
        cache.put(p, q.query, result)
        return result

    return app


app = None  # created by `uvicorn rag.api:create_app --factory`

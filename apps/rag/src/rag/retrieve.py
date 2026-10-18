"""Retrieval: ACL-filtered vector search, rerank, then a second ACL check.

Book: Sovereign AI Stacks, Chapter 8
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from qdrant_client import QdrantClient

from rag.acl import collection_for, may_read, read_filter
from rag.auth import Principal
from rag.clients import Embedder, Reranker


@dataclass(frozen=True)
class Passage:
    text: str
    source: str
    heading: str
    doc_id: str
    score: float


class Retriever:
    def __init__(self, qdrant: QdrantClient, embedder: Embedder, reranker: Reranker | None) -> None:
        self.qdrant = qdrant
        self.embedder = embedder
        self.reranker = reranker

    def search(self, p: Principal, query: str, candidates: int = 40, top_k: int = 6) -> list[Passage]:
        name = collection_for(p.tenant)
        if not self.qdrant.collection_exists(name):
            return []
        vector = self.embedder.embed([query])[0]
        hits = self.qdrant.query_points(
            name, query=vector, query_filter=read_filter(p), limit=candidates, with_payload=True
        ).points
        # Defence in depth: re-check every hit in Python. If the filter were
        # ever wrong, this drops the chunk instead of leaking it.
        allowed = [h for h in hits if may_read(p, h.payload or {})]
        if self.reranker and allowed:
            order = self.reranker.rerank(query, [h.payload["text"] for h in allowed])
            ranked = [(allowed[i], s) for i, s in order]
        else:
            ranked = [(h, h.score) for h in allowed]
        return [
            Passage(
                text=h.payload["text"],
                source=h.payload.get("source", ""),
                heading=h.payload.get("heading", ""),
                doc_id=h.payload.get("doc_id", ""),
                score=float(s),
            )
            for h, s in ranked[:top_k]
        ]


class AnswerCache:
    """Short-lived answer cache. The key includes the caller's ACL scope.

    Chapter 8's war story: a cache keyed on the question alone served a
    confidential answer to a user without clearance. Two users who may see
    different documents must never share a cache entry.
    """

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 2048) -> None:
        self.ttl = ttl_seconds
        self.max = max_entries
        self._data: dict[tuple, tuple[float, object]] = {}

    @staticmethod
    def key(p: Principal, query: str) -> tuple:
        return (*p.acl_scope, " ".join(query.lower().split()))

    def get(self, p: Principal, query: str) -> object | None:
        k = self.key(p, query)
        hit = self._data.get(k)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        self._data.pop(k, None)
        return None

    def put(self, p: Principal, query: str, value: object) -> None:
        if len(self._data) >= self.max:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest)
        self._data[self.key(p, query)] = (time.monotonic(), value)

"""Shared fixtures: in-memory Qdrant, a deterministic embedder, sample documents."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from rag.auth import Principal
from rag.ingest import EMBED_DIM, ingest


class HashEmbedder:
    """Bag-of-words hashed into 1024 dimensions: similar words -> similar vectors."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode(), usedforsecurity=False).hexdigest(), 16) % EMBED_DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def principal(tenant: str, clearance: int = 0, groups: tuple[str, ...] = (), sub: str = "u1") -> Principal:
    return Principal(subject=sub, tenant=tenant, clearance=clearance, groups=groups, client_id="test")


@pytest.fixture
def store(tmp_path: Path) -> QdrantClient:
    client = QdrantClient(":memory:")
    emb = HashEmbedder()
    docs = {
        "mh-public.md": ("ministry-health", "public", [], "# Clinic hours\n\nClinics open at 08:00 on weekdays."),
        "mh-conf.md": (
            "ministry-health",
            "confidential",
            [],
            "# Referral backlog\n\nThe referral backlog at the regional hospital is 4,200 patients.",
        ),
        "mh-hr.md": (
            "ministry-health",
            "restricted",
            ["hr-directorate"],
            "# Salary review\n\nThe referral backlog bonus scheme applies to HR staff only.",
        ),
        "ra-public.md": (
            "revenue-authority",
            "public",
            [],
            "# Filing deadlines\n\nThe referral backlog of tax appeals is published monthly.",
        ),
    }
    for name, (tenant, cls, groups, text) in docs.items():
        f = tmp_path / name
        f.write_text(text, encoding="utf-8")
        ingest(client, emb, f, tenant, cls, groups, source=name)
    return client

"""Document-level access control, enforced inside the vector database query.

Book: Sovereign AI Stacks, Chapter 8

Every chunk stored in Qdrant carries:
    tenant          str    owning tenant (also implied by the collection name)
    classification  int    0 public .. 3 secret (see auth.CLEARANCE_LEVELS)
    groups_allowed  list   optional; if non-empty, reader must be in one of them

The filter is applied by Qdrant during search, so a chunk the caller may not
read is never returned, never reranked and never placed in a prompt.
"""

from __future__ import annotations

import re

from qdrant_client import models

from rag.auth import Principal

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def collection_for(tenant: str) -> str:
    """One collection per tenant. The name is derived, never user-supplied."""
    if not _TENANT_RE.fullmatch(tenant):
        raise ValueError(f"invalid tenant id {tenant!r}")
    return f"docs_{tenant.replace('-', '_')}"


def read_filter(p: Principal) -> models.Filter:
    """Qdrant filter for everything `p` may read."""
    return models.Filter(
        must=[
            models.FieldCondition(key="tenant", match=models.MatchValue(value=p.tenant)),
            models.FieldCondition(key="classification", range=models.Range(lte=p.clearance)),
            models.Filter(
                should=[
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="groups_allowed")),
                    models.FieldCondition(key="groups_allowed", match=models.MatchAny(any=list(p.groups) or ["-"])),
                ]
            ),
        ]
    )


def may_read(p: Principal, payload: dict) -> bool:
    """The same rule in Python, used as a second check on every returned chunk."""
    if payload.get("tenant") != p.tenant:
        return False
    if int(payload.get("classification", 99)) > p.clearance:
        return False
    allowed = payload.get("groups_allowed") or []
    return not allowed or bool(set(allowed) & set(p.groups))

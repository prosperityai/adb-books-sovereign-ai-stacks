"""The tenant boundary and document ACLs, tested against a real (in-memory) Qdrant."""

from __future__ import annotations

import pytest
from conftest import HashEmbedder, principal
from rag.acl import collection_for, may_read
from rag.retrieve import AnswerCache, Retriever


@pytest.fixture
def retriever(store):
    return Retriever(store, HashEmbedder(), reranker=None)


def sources(passages) -> set[str]:
    return {p.source for p in passages}


def test_tenant_isolation(retriever) -> None:
    # Every document mentions "referral backlog"; only the caller's tenant may match.
    ra = retriever.search(principal("revenue-authority", clearance=3), "referral backlog")
    assert sources(ra) == {"ra-public.md"}


def test_clearance_filters_documents(retriever) -> None:
    public_user = retriever.search(principal("ministry-health", clearance=0), "referral backlog")
    assert "mh-conf.md" not in sources(public_user)
    cleared = retriever.search(principal("ministry-health", clearance=2), "referral backlog")
    assert "mh-conf.md" in sources(cleared)


def test_group_restricted_documents(retriever) -> None:
    outsider = retriever.search(principal("ministry-health", clearance=3), "bonus scheme")
    assert "mh-hr.md" not in sources(outsider)
    hr = retriever.search(principal("ministry-health", clearance=1, groups=("hr-directorate",)), "bonus scheme")
    assert "mh-hr.md" in sources(hr)


def test_unknown_tenant_has_no_collection(retriever) -> None:
    assert retriever.search(principal("ministry-defence", clearance=3), "referral backlog") == []


@pytest.mark.parametrize("bad", ["../etc", "Ministry Health", "a", "x" * 70, "docs_*"])
def test_collection_names_are_validated(bad: str) -> None:
    with pytest.raises(ValueError):
        collection_for(bad)


def test_python_check_matches_filter() -> None:
    p = principal("ministry-health", clearance=1, groups=("hr-directorate",))
    assert may_read(p, {"tenant": "ministry-health", "classification": 1, "groups_allowed": ["hr-directorate"]})
    assert not may_read(p, {"tenant": "revenue-authority", "classification": 0, "groups_allowed": []})
    assert not may_read(p, {"tenant": "ministry-health", "classification": 2, "groups_allowed": []})
    assert not may_read(p, {"tenant": "ministry-health", "classification": 0, "groups_allowed": ["finance"]})
    assert not may_read(p, {"tenant": "ministry-health"})  # missing classification = deny


def test_cache_key_includes_acl_scope() -> None:
    cache = AnswerCache()
    cleared = principal("ministry-health", clearance=2, sub="alice")
    public = principal("ministry-health", clearance=0, sub="bob")
    cache.put(cleared, "What is the referral backlog?", "4,200 patients")
    assert cache.get(cleared, "what is the  referral backlog?") == "4,200 patients"
    assert cache.get(public, "What is the referral backlog?") is None

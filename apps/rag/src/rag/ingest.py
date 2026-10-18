"""Ingestion: convert -> chunk -> embed -> upsert, with ACL metadata on every chunk.

Book: Sovereign AI Stacks, Chapter 8

    rag-ingest --tenant ministry-health --classification restricted \
        --groups hr-directorate --source "HR Manual 2026" /import/hr-manual.pdf

PDF, DOCX, PPTX and HTML are converted with Docling (install the "ingest"
extra); Markdown and plain text are read directly.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient, models

from rag.acl import collection_for
from rag.auth import clearance_rank
from rag.clients import Embedder

EMBED_DIM = 1024  # BAAI/bge-m3 dense output
MAX_CHARS = 1800  # ~450 tokens for English; Swahili and Arabic run a little longer per token
OVERLAP = 200
_NAMESPACE = uuid.UUID("6f2b9a1e-6d4b-4b8e-9a7e-2f1d5c3b8a90")


@dataclass(frozen=True)
class Chunk:
    text: str
    heading: str
    ordinal: int


def to_markdown(path: Path) -> str:
    """Return document text as Markdown, preserving headings for chunking."""
    if path.suffix.lower() in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8")
    from docling.document_converter import DocumentConverter  # heavy import, ingest jobs only

    return DocumentConverter().convert(str(path)).document.export_to_markdown()


def chunk_markdown(md: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[Chunk]:
    """Split on headings first, then on paragraph boundaries within a section.

    Keeping each chunk inside one section means a retrieved chunk carries the
    heading it belongs to, which is what makes citations useful to a reader.
    """
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in md.splitlines():
        if re.match(r"^#{1,6}\s", line):
            if any(s.strip() for s in buf):
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    if any(s.strip() for s in buf):
        sections.append((heading, "\n".join(buf).strip()))

    chunks: list[Chunk] = []
    for head, body in sections:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        current = ""
        for para in paragraphs:
            while len(para) > max_chars:  # a single huge paragraph: hard split with overlap
                if current:
                    chunks.append(Chunk(current, head, len(chunks)))
                    current = ""
                chunks.append(Chunk(para[:max_chars], head, len(chunks)))
                para = para[max_chars - overlap :]
            if len(current) + len(para) + 2 > max_chars and current:
                chunks.append(Chunk(current, head, len(chunks)))
                current = current[-overlap:] + "\n\n" + para
            else:
                current = f"{current}\n\n{para}" if current else para
        if current:
            chunks.append(Chunk(current, head, len(chunks)))
    return chunks


def ensure_collection(client: QdrantClient, tenant: str) -> str:
    name = collection_for(tenant)
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
        )
        # Indexes on the ACL fields make filtered search fast and exact.
        client.create_payload_index(name, "tenant", models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "classification", models.PayloadSchemaType.INTEGER)
        client.create_payload_index(name, "groups_allowed", models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "doc_id", models.PayloadSchemaType.KEYWORD)
    return name


def batched(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def ingest(
    client: QdrantClient,
    embedder: Embedder,
    path: Path,
    tenant: str,
    classification: str,
    groups: list[str],
    source: str,
) -> int:
    """Ingest one document; re-ingesting the same file replaces its chunks."""
    raw = path.read_bytes()
    doc_id = hashlib.sha256(raw).hexdigest()[:24]
    level = clearance_rank(classification)
    chunks = chunk_markdown(to_markdown(path))
    name = ensure_collection(client, tenant)
    # Replace, never duplicate: remove earlier chunks of this document first.
    client.delete(
        name,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        ),
    )
    for batch in batched(chunks, 32):
        vectors = embedder.embed([f"{c.heading}\n{c.text}" if c.heading else c.text for c in batch])
        client.upsert(
            name,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid5(_NAMESPACE, f"{doc_id}:{c.ordinal}")),
                    vector=v,
                    payload={
                        "tenant": tenant,
                        "classification": level,
                        "groups_allowed": sorted(groups),
                        "doc_id": doc_id,
                        "source": source,
                        "file": path.name,
                        "heading": c.heading,
                        "ordinal": c.ordinal,
                        "text": c.text,
                    },
                )
                for c, v in zip(batch, vectors, strict=True)
            ],
        )
    return len(chunks)


def main(argv: list[str] | None = None) -> int:
    import os

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--tenant", required=True)
    p.add_argument("--classification", required=True, choices=["public", "restricted", "confidential", "secret"])
    p.add_argument("--groups", nargs="*", default=[])
    p.add_argument("--source", required=True, help="human-readable source name used in citations")
    args = p.parse_args(argv)
    client = QdrantClient(
        url=os.environ.get("QDRANT_URL", "http://qdrant.rag.svc:6333"), api_key=os.environ["QDRANT_API_KEY"]
    )
    embedder = Embedder(os.environ.get("TEI_EMBED_URL", "http://tei-bge-m3.rag.svc:8080"), os.environ["TEI_API_KEY"])
    for f in args.files:
        n = ingest(client, embedder, f, args.tenant, args.classification, args.groups, args.source)
        print(f"{f.name}: {n} chunks -> {collection_for(args.tenant)} ({args.classification})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

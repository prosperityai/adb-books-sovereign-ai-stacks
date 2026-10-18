#!/usr/bin/env python3
"""fetch_models.py — download pinned models and prove what you downloaded.

Book: Sovereign AI Stacks, Chapter 3
Runs on: the CONNECTED transfer station only.
Requires: Python 3.13, huggingface_hub 2.2, PyYAML 6.0

For each model in airgap/models.yaml this script:

1. Downloads exactly the pinned revision (commit SHA) with only the allowed files.
2. Hashes every file with SHA-256.
3. Cross-checks every file against the hash the Hub itself records: the
   SHA-256 in the Git LFS pointer for large files, or the Git blob SHA-1 for
   small files stored directly in Git. A mismatch means the download is
   corrupt or was tampered with, and the script fails.
4. Writes MANIFEST.sha256 (sha256sum -c compatible) and model-info.json.

Usage:
    python fetch_models.py --catalogue ../models.yaml --out /transfer/models
    python fetch_models.py --catalogue ../models.yaml --out /transfer/models --only bge-m3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from huggingface_hub import HfApi, snapshot_download

CHUNK = 16 * 1024 * 1024  # 16 MiB reads keep memory flat on 50 GB files


def sha256_file(path: Path) -> str:
    """Stream a file through SHA-256."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def git_blob_sha1(path: Path) -> str:
    """Git's object id for a file: SHA-1 over 'blob <size>\\0' + content."""
    h = hashlib.sha1(usedforsecurity=False)
    h.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def upstream_hashes(repo: str, revision: str) -> dict[str, tuple[str, str]]:
    """Map repo-relative path -> (algorithm, hash) as recorded by the Hub.

    LFS files carry a SHA-256 of their content; files stored directly in Git
    carry the Git blob SHA-1.
    """
    info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    hashes: dict[str, tuple[str, str]] = {}
    for sibling in info.siblings or []:
        lfs = getattr(sibling, "lfs", None)
        if lfs is not None and getattr(lfs, "sha256", None):
            hashes[sibling.rfilename] = ("sha256", lfs.sha256)
        elif getattr(sibling, "blob_id", None):
            hashes[sibling.rfilename] = ("git-sha1", sibling.blob_id)
    return hashes


def fetch_one(model: dict, out_root: Path) -> dict:
    """Download, hash and verify one model. Returns its info record."""
    name, repo, revision = model["name"], model["repo"], model["revision"]
    if len(revision) != 40:
        raise ValueError(f"{name}: revision must be a full 40-character commit SHA, got {revision!r}")

    target = out_root / name
    print(f"[{name}] downloading {repo}@{revision[:12]}", flush=True)
    snapshot_download(
        repo_id=repo,
        revision=revision,
        allow_patterns=model.get("allow_patterns"),
        local_dir=target,
    )

    # Hugging Face writes a .cache/ folder inside local_dir; it is not part of the model.
    files = sorted(p for p in target.rglob("*") if p.is_file() and ".cache" not in p.relative_to(target).parts)
    if not files:
        raise RuntimeError(f"{name}: no files matched allow_patterns {model.get('allow_patterns')}")

    upstream = upstream_hashes(repo, revision)
    lines: list[str] = []
    total = 0
    verified = 0
    for path in files:
        rel = path.relative_to(target).as_posix()
        digest = sha256_file(path)
        total += path.stat().st_size
        if rel not in upstream:
            raise RuntimeError(f"{name}: {rel} is not part of {repo}@{revision}")
        algorithm, expected = upstream[rel]
        actual = digest if algorithm == "sha256" else git_blob_sha1(path)
        if actual != expected:
            raise RuntimeError(f"{name}: {rel} {algorithm} {actual} != upstream {expected}")
        verified += 1
        lines.append(f"{digest}  {rel}")

    (target / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    record = {
        "name": name,
        "repo": repo,
        "revision": revision,
        "licence": model.get("licence"),
        "purpose": model.get("purpose"),
        "files": len(files),
        "bytes": total,
        "files_verified_against_hub": verified,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (target / "model-info.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(
        f"[{name}] {len(files)} files, {total / 1e9:.2f} GB, {verified}/{len(files)} verified against Hub hashes",
        flush=True,
    )
    return record


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalogue", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--only", nargs="*", help="fetch only these model names")
    args = p.parse_args(argv)

    catalogue = yaml.safe_load(args.catalogue.read_text(encoding="utf-8"))
    models = catalogue["models"]
    if args.only:
        unknown = set(args.only) - {m["name"] for m in models}
        if unknown:
            print(f"unknown model(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        models = [m for m in models if m["name"] in args.only]

    args.out.mkdir(parents=True, exist_ok=True)
    records = [fetch_one(m, args.out) for m in models]
    (args.out / "models-index.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

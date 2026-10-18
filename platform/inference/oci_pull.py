#!/usr/bin/env python3
r"""oci_pull.py — fetch a model stored as an OCI artifact and verify every byte.

Book: Sovereign AI Stacks, Chapter 5
Requires: Python 3.10+ standard library only (runs inside the vLLM image).

Runs as an init container before vLLM starts. It:

1. Resolves the reference (tag or digest) to a manifest digest.
2. Skips the download if the cache already holds exactly that digest.
3. Streams each layer to disk while hashing it; an OCI blob digest IS the
   SHA-256 of its content, so a byte that changed anywhere fails the pull.
4. Re-checks the model's own MANIFEST.sha256 (written at fetch time, Chapter 3).
5. Swaps the verified directory into place atomically and records the digest.

Usage:
    python3 oci_pull.py harbor.enclave.internal/models/qwen3.8-27b-fp8:017b9c7af6b5 /models/qwen3.8-27b-fp8 \
        --docker-config /auth/.dockerconfigjson --ca-file /ca/ca.crt [--expect-digest sha256:...]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
CHUNK = 16 * 1024 * 1024
MARKER = ".oci-digest"


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    reference: str  # tag or sha256:<hex>

    @property
    def is_digest(self) -> bool:
        return self.reference.startswith("sha256:")


def parse_reference(ref: str) -> Reference:
    """Split 'host[:port]/path/name(:tag|@sha256:hex)' into its parts."""
    if "/" not in ref:
        raise ValueError(f"reference must include a registry host: {ref!r}")
    registry, rest = ref.split("/", 1)
    if "@" in rest:
        repository, reference = rest.split("@", 1)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
            raise ValueError(f"invalid digest in {ref!r}")
    elif ":" in rest.rsplit("/", 1)[-1]:
        repository, reference = rest.rsplit(":", 1)
    else:
        raise ValueError(f"reference must pin a tag or digest, refusing implicit 'latest': {ref!r}")
    return Reference(registry, repository, reference)


def credentials_from_docker_config(path: Path | None, registry: str) -> tuple[str, str] | None:
    """Read username/password for `registry` from a dockerconfigjson file."""
    if path is None or not path.exists():
        return None
    auths = json.loads(path.read_text(encoding="utf-8")).get("auths", {})
    entry = auths.get(registry) or auths.get(f"https://{registry}")
    if not entry:
        return None
    if "auth" in entry:
        user, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
        return user, password
    return entry.get("username", ""), entry.get("password", "")


class RegistryClient:
    """Minimal OCI distribution client: manifests and blobs, Basic or Bearer auth."""

    def __init__(self, registry: str, creds: tuple[str, str] | None, ca_file: Path | None, plain_http: bool) -> None:
        self.base = f"{'http' if plain_http else 'https'}://{registry}"
        self.creds = creds
        self.context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        self.auth_header: str | None = None

    def _open(self, url: str, accept: str | None = None, retry: bool = True):
        req = urllib.request.Request(url)  # noqa: S310 — scheme is fixed to http(s) above
        if accept:
            req.add_header("Accept", accept)
        if self.auth_header:
            req.add_header("Authorization", self.auth_header)
        try:
            return urllib.request.urlopen(req, context=self.context, timeout=60)  # noqa: S310
        except urllib.error.HTTPError as err:
            if err.code == 401 and retry:
                self._authenticate(err.headers.get("WWW-Authenticate", ""))
                return self._open(url, accept, retry=False)
            raise

    def _authenticate(self, challenge: str) -> None:
        """Answer a Basic or Bearer (token service) challenge."""
        scheme, _, params = challenge.partition(" ")
        basic = None
        if self.creds:
            basic = "Basic " + base64.b64encode(f"{self.creds[0]}:{self.creds[1]}".encode()).decode()
        if scheme.lower() == "basic":
            if basic is None:
                raise PermissionError("registry requires credentials and none were provided")
            self.auth_header = basic
            return
        if scheme.lower() != "bearer":
            raise PermissionError(f"unsupported auth challenge: {challenge!r}")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
        query = {k: v for k, v in fields.items() if k in ("service", "scope")}
        token_url = fields["realm"] + "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(token_url)  # noqa: S310
        if basic:
            req.add_header("Authorization", basic)
        with urllib.request.urlopen(req, context=self.context, timeout=60) as resp:  # noqa: S310
            body = json.load(resp)
        self.auth_header = "Bearer " + (body.get("token") or body["access_token"])

    def manifest(self, ref: Reference) -> tuple[dict, str]:
        url = f"{self.base}/v2/{ref.repository}/manifests/{ref.reference}"
        with self._open(url, accept=MANIFEST_TYPES) as resp:
            raw = resp.read()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if ref.is_digest and digest != ref.reference:
            raise ValueError(f"manifest digest {digest} does not match requested {ref.reference}")
        return json.loads(raw), digest

    def blob(self, ref: Reference, digest: str, dest: Path) -> None:
        """Stream a blob to `dest`, failing if its SHA-256 differs from `digest`."""
        url = f"{self.base}/v2/{ref.repository}/blobs/{digest}"
        h = hashlib.sha256()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._open(url) as resp, dest.open("wb") as out:
            while block := resp.read(CHUNK):
                h.update(block)
                out.write(block)
        actual = "sha256:" + h.hexdigest()
        if actual != digest:
            dest.unlink(missing_ok=True)
            raise ValueError(f"blob {digest} arrived as {actual}: corrupt or tampered")


def safe_relative_path(title: str) -> PurePosixPath:
    """Reject layer titles that would escape the model directory."""
    p = PurePosixPath(title)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"unsafe layer path {title!r}")
    return p


def verify_manifest_file(model_dir: Path) -> int:
    """Check MANIFEST.sha256 (sha256sum format). Returns number of files verified."""
    manifest = model_dir / "MANIFEST.sha256"
    if not manifest.exists():
        return 0
    count = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, rel = line.split(maxsplit=1)
        path = model_dir / safe_relative_path(rel.strip())
        h = hashlib.sha256()
        with path.open("rb") as f:
            while block := f.read(CHUNK):
                h.update(block)
        if h.hexdigest() != expected:
            raise ValueError(f"{rel}: MANIFEST.sha256 mismatch")
        count += 1
    return count


def pull(
    ref_str: str,
    out_dir: Path,
    docker_config: Path | None = None,
    ca_file: Path | None = None,
    plain_http: bool = False,
    expect_digest: str | None = None,
) -> str:
    """Pull `ref_str` into `out_dir`. Returns the manifest digest."""
    ref = parse_reference(ref_str)
    client = RegistryClient(
        ref.registry, credentials_from_docker_config(docker_config, ref.registry), ca_file, plain_http
    )
    manifest, digest = client.manifest(ref)
    if expect_digest and digest != expect_digest:
        raise ValueError(f"{ref_str} resolved to {digest}, expected {expect_digest}")

    marker = out_dir / MARKER
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
        print(f"cache hit: {out_dir} already holds {digest}", flush=True)
        return digest

    staging = out_dir.with_name(out_dir.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    layers = manifest.get("layers", [])
    if not layers:
        raise ValueError(f"{ref_str} has no layers")
    for i, layer in enumerate(layers, 1):
        title = layer.get("annotations", {}).get("org.opencontainers.image.title")
        if not title:
            raise ValueError(f"layer {layer['digest']} has no title annotation")
        rel = safe_relative_path(title)
        print(f"[{i}/{len(layers)}] {rel} ({layer['size'] / 1e9:.2f} GB)", flush=True)
        client.blob(ref, layer["digest"], staging / rel)

    verified = verify_manifest_file(staging)
    print(f"verified {len(layers)} layer digests and {verified} MANIFEST.sha256 entries", flush=True)

    # Swap: never leave a half-written model where vLLM would load it.
    backup = out_dir.with_name(out_dir.name + ".old")
    shutil.rmtree(backup, ignore_errors=True)
    if out_dir.exists():
        out_dir.rename(backup)
    staging.rename(out_dir)
    shutil.rmtree(backup, ignore_errors=True)
    (out_dir / MARKER).write_text(digest + "\n", encoding="utf-8")
    print(f"ready: {out_dir} = {digest}", flush=True)
    return digest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reference")
    p.add_argument("out_dir", type=Path)
    p.add_argument("--docker-config", type=Path)
    p.add_argument("--ca-file", type=Path)
    p.add_argument("--plain-http", action="store_true", help="lab registries only")
    p.add_argument("--expect-digest")
    args = p.parse_args(argv)
    try:
        pull(args.reference, args.out_dir, args.docker_config, args.ca_file, args.plain_http, args.expect_digest)
    except (ValueError, PermissionError, urllib.error.URLError) as err:
        print(f"PULL FAILED: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

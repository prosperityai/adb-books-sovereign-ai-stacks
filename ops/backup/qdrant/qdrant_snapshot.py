#!/usr/bin/env python3
"""qdrant_snapshot.py — per-node Qdrant snapshots to the off-cluster backup appliance.

Book: Sovereign AI Stacks, Chapter 12
Requires: Python 3.13 standard library only (runs in python:3.13.16-slim-trixie)
Qdrant: 1.19.x REST API.  Object store: any S3-compatible appliance (SigV4, path-style).

    qdrant_snapshot.py backup             # snapshot every collection on every node, upload, prune
    qdrant_snapshot.py verify [--run ID]  # re-download and re-hash every object of a run
    qdrant_snapshot.py restore --run ID [--collection NAME] [--node N]

Configuration comes from the environment (see qdrant-snapshot.yaml):
  QDRANT_NODES        comma-separated base URLs, one per StatefulSet pod, in pod order
  QDRANT_API_KEY      Qdrant API key
  S3_ENDPOINT         https://backup.enclave.internal
  S3_BUCKET, S3_PREFIX, S3_REGION (default us-east-1), S3_CA_FILE
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  WORKDIR             scratch directory for one snapshot at a time

In a distributed deployment a collection snapshot holds only the shards on the
node that took it, so every node is snapshotted separately and restored to the
node with the same index. A run is complete only when its MANIFEST.json exists:
the manifest is written last, so a run that died half-way is never "latest".
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import ssl
import sys
import urllib.parse
import uuid
import xml.etree.ElementTree as ET  # noqa: S405 — parses replies from our own appliance only
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CHUNK = 8 * 1024 * 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


# --- AWS Signature Version 4 ---------------------------------------------------
def _uri_encode(value: str, keep_slash: bool) -> str:
    return urllib.parse.quote(value, safe="/~" if keep_slash else "~")


def sign_v4(
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
) -> str:
    """Return the Authorization header value for an S3 request (SigV4, header auth)."""
    date = amz_date[:8]
    hdrs = {k.lower(): " ".join(v.split()) for k, v in headers.items()}
    hdrs["host"] = host
    hdrs["x-amz-date"] = amz_date
    hdrs["x-amz-content-sha256"] = payload_sha256
    signed = ";".join(sorted(hdrs))
    canonical = "\n".join(
        [
            method,
            _uri_encode(path, keep_slash=True),
            "&".join(f"{_uri_encode(k, False)}={_uri_encode(v, False)}" for k, v in sorted(query.items())),
            "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs)),
            signed,
            payload_sha256,
        ]
    )
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret_key}".encode()
    for part in (date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope},SignedHeaders={signed},Signature={signature}"


def _connection(url: urllib.parse.SplitResult, ca_file: str | None) -> http.client.HTTPConnection:
    if url.scheme == "https":
        ctx = ssl.create_default_context(cafile=ca_file)
        return http.client.HTTPSConnection(url.hostname or "", url.port or 443, context=ctx, timeout=300)
    return http.client.HTTPConnection(url.hostname or "", url.port or 80, timeout=300)


class S3:
    """Just enough S3 for backups: PUT (single and multipart), GET, with SigV4."""

    part_size = 64 * 1024 * 1024

    def __init__(self, endpoint: str, bucket: str, region: str, access: str, secret: str, ca: str | None) -> None:
        self.url = urllib.parse.urlsplit(endpoint)
        self.bucket, self.region, self.access, self.secret, self.ca = bucket, region, access, secret, ca

    def _request(
        self,
        method: str,
        key: str,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        stream: Callable[[http.client.HTTPResponse], object] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        query = query or {}
        path = f"/{self.bucket}/{key}"
        host = self.url.netloc
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = hashlib.sha256(body).hexdigest()
        auth = sign_v4(method, host, path, query, {}, payload, self.access, self.secret, self.region, amz_date)
        qs = "&".join(f"{_uri_encode(k, False)}={_uri_encode(v, False)}" for k, v in sorted(query.items()))
        conn = _connection(self.url, self.ca)
        try:
            conn.request(
                method,
                _uri_encode(path, keep_slash=True) + (f"?{qs}" if qs else ""),
                body=body or None,
                headers={
                    "Host": host,
                    "x-amz-date": amz_date,
                    "x-amz-content-sha256": payload,
                    "Authorization": auth,
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            if stream is not None and resp.status == 200:
                stream(resp)
                data = b""
            else:
                data = resp.read()
            if resp.status >= 300:
                raise RuntimeError(f"S3 {method} {path} -> HTTP {resp.status}: {data[:300]!r}")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def put_bytes(self, key: str, data: bytes) -> None:
        self._request("PUT", key, body=data)

    def get_bytes(self, key: str) -> bytes:
        return self._request("GET", key)[2]

    def upload_file(self, key: str, path: Path) -> None:
        size = path.stat().st_size
        if size <= self.part_size:
            self.put_bytes(key, path.read_bytes())
            return
        _, _, data = self._request("POST", key, {"uploads": ""})
        upload_id = _xml_text(data, "UploadId")
        parts: list[tuple[int, str]] = []
        try:
            with path.open("rb") as f:
                number = 1
                while block := f.read(self.part_size):
                    _, headers, _ = self._request(
                        "PUT", key, {"partNumber": str(number), "uploadId": upload_id}, body=block
                    )
                    parts.append((number, headers["etag"]))
                    number += 1
            xml = "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in parts)
            body = f"<CompleteMultipartUpload>{xml}</CompleteMultipartUpload>".encode()
            self._request("POST", key, {"uploadId": upload_id}, body=body)
        except Exception:
            self._request("DELETE", key, {"uploadId": upload_id})
            raise

    def stream_sha256(self, key: str, dest: Path | None = None) -> tuple[str, int]:
        """Download `key`, hashing as it streams; optionally keep it at `dest`."""
        h = hashlib.sha256()
        size = 0

        def sink(resp: http.client.HTTPResponse) -> None:
            nonlocal size
            out = dest.open("wb") if dest else None
            try:
                while block := resp.read(CHUNK):
                    h.update(block)
                    size += len(block)
                    if out:
                        out.write(block)
            finally:
                if out:
                    out.close()

        self._request("GET", key, stream=sink)
        return h.hexdigest(), size


def _xml_text(data: bytes, tag: str) -> str:
    root = ET.fromstring(data)  # noqa: S314 — trusted appliance response
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag and el.text:
            return el.text
    raise RuntimeError(f"no <{tag}> in S3 response")


# --- Qdrant ------------------------------------------------------------------------
class Qdrant:
    def __init__(self, base: str, api_key: str) -> None:
        self.url = urllib.parse.urlsplit(base)
        self.api_key = api_key

    def _conn(self) -> http.client.HTTPConnection:
        return _connection(self.url, None)

    def _json(self, method: str, path: str) -> dict:
        conn = self._conn()
        try:
            conn.request(method, path, headers={"api-key": self.api_key})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 300:
                raise RuntimeError(f"Qdrant {method} {path} -> HTTP {resp.status}: {body[:300]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def collections(self) -> list[str]:
        return sorted(c["name"] for c in self._json("GET", "/collections")["result"]["collections"])

    def create_snapshot(self, collection: str) -> dict:
        return self._json("POST", f"/collections/{_uri_encode(collection, False)}/snapshots?wait=true")["result"]

    def delete_snapshot(self, collection: str, name: str) -> None:
        self._json("DELETE", f"/collections/{_uri_encode(collection, False)}/snapshots/{_uri_encode(name, False)}")

    def download_snapshot(self, collection: str, name: str, dest: Path) -> str:
        conn = self._conn()
        h = hashlib.sha256()
        try:
            path = f"/collections/{_uri_encode(collection, False)}/snapshots/{_uri_encode(name, False)}"
            conn.request("GET", path, headers={"api-key": self.api_key})
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"Qdrant snapshot download -> HTTP {resp.status}")
            with dest.open("wb") as out:
                while block := resp.read(CHUNK):
                    h.update(block)
                    out.write(block)
        finally:
            conn.close()
        return h.hexdigest()

    def upload_snapshot(self, collection: str, src: Path) -> None:
        """Recover a collection on THIS node from a snapshot file (multipart/form-data, streamed)."""
        boundary = uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="snapshot"; filename="{src.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        conn = self._conn()
        try:
            conn.putrequest("POST", f"/collections/{_uri_encode(collection, False)}/snapshots/upload?priority=snapshot")
            conn.putheader("api-key", self.api_key)
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(len(head) + src.stat().st_size + len(tail)))
            conn.endheaders()
            conn.send(head)
            with src.open("rb") as f:
                while block := f.read(CHUNK):
                    conn.send(block)
            conn.send(tail)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 300:
                raise RuntimeError(f"Qdrant snapshot upload -> HTTP {resp.status}: {body[:300]!r}")
        finally:
            conn.close()


# --- operations ----------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    nodes: list[str]
    api_key: str
    prefix: str
    workdir: Path

    @staticmethod
    def from_env() -> Config:
        nodes = [n.strip() for n in os.environ.get("QDRANT_NODES", "").split(",") if n.strip()]
        if not nodes:
            raise SystemExit("QDRANT_NODES is empty")
        return Config(
            nodes,
            os.environ.get("QDRANT_API_KEY", ""),
            os.environ.get("S3_PREFIX", "qdrant").strip("/"),
            Path(os.environ.get("WORKDIR", "/work")),
        )


def s3_from_env() -> S3:
    return S3(
        os.environ["S3_ENDPOINT"],
        os.environ["S3_BUCKET"],
        os.environ.get("S3_REGION", "us-east-1"),
        os.environ["AWS_ACCESS_KEY_ID"],
        os.environ["AWS_SECRET_ACCESS_KEY"],
        os.environ.get("S3_CA_FILE") or None,
    )


def backup(cfg: Config, s3: S3, clients: list[Qdrant], now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    run = now.strftime("%Y%m%dT%H%MZ")
    entries = []
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    for index, q in enumerate(clients):
        for coll in q.collections():
            snap = q.create_snapshot(coll)
            local = cfg.workdir / snap["name"]
            try:
                digest = q.download_snapshot(coll, snap["name"], local)
                if snap.get("checksum") and snap["checksum"] != digest:
                    raise RuntimeError(f"node {index} {coll}: download hash differs from Qdrant's checksum")
                key = f"{cfg.prefix}/{run}/node-{index}/{coll}/{snap['name']}"
                s3.upload_file(key, local)
                entries.append(
                    {"node": index, "collection": coll, "key": key, "sha256": digest, "size": local.stat().st_size}
                )
                print(f"node-{index} {coll}: {local.stat().st_size} bytes -> {key}", flush=True)
            finally:
                local.unlink(missing_ok=True)
                q.delete_snapshot(coll, snap["name"])  # the snapshot PVC is not a backup
    manifest = {"run": run, "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "nodes": len(clients), "items": entries}
    s3.put_bytes(f"{cfg.prefix}/{run}/MANIFEST.json", json.dumps(manifest, indent=2).encode())
    s3.put_bytes(f"{cfg.prefix}/LATEST", run.encode())
    return manifest


def load_manifest(cfg: Config, s3: S3, run: str | None) -> dict:
    run = run or s3.get_bytes(f"{cfg.prefix}/LATEST").decode().strip()
    return json.loads(s3.get_bytes(f"{cfg.prefix}/{run}/MANIFEST.json"))


def verify(cfg: Config, s3: S3, run: str | None) -> dict:
    manifest = load_manifest(cfg, s3, run)
    bad = []
    for item in manifest["items"]:
        digest, size = s3.stream_sha256(item["key"])
        if digest != item["sha256"] or size != item["size"]:
            bad.append(item["key"])
    return {
        "run": manifest["run"],
        "created_at": manifest["created_at"],
        "verified": len(manifest["items"]) - len(bad),
        "corrupt": bad,
    }


def restore(cfg: Config, s3: S3, clients: list[Qdrant], run: str, collection: str | None, node: int | None) -> int:
    manifest = load_manifest(cfg, s3, run)
    if manifest["nodes"] != len(clients):
        raise SystemExit(f"run {run} was taken from {manifest['nodes']} nodes; QDRANT_NODES lists {len(clients)}")
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    done = 0
    for item in _select(manifest["items"], collection, node):
        local = cfg.workdir / Path(item["key"]).name
        try:
            digest, _ = s3.stream_sha256(item["key"], local)
            if digest != item["sha256"]:
                raise SystemExit(f"{item['key']}: hash mismatch, refusing to restore")
            clients[item["node"]].upload_snapshot(item["collection"], local)
            print(f"restored {item['collection']} on node-{item['node']} from {item['key']}", flush=True)
            done += 1
        finally:
            local.unlink(missing_ok=True)
    return done


def _select(items: list[dict], collection: str | None, node: int | None) -> Iterator[dict]:
    for item in items:
        if (collection is None or item["collection"] == collection) and (node is None or item["node"] == node):
            yield item


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Qdrant snapshots to the backup appliance.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    v = sub.add_parser("verify")
    v.add_argument("--run")
    r = sub.add_parser("restore")
    r.add_argument("--run", required=True)
    r.add_argument("--collection")
    r.add_argument("--node", type=int)
    a = p.parse_args(argv)
    cfg, s3 = Config.from_env(), s3_from_env()
    clients = [Qdrant(n, cfg.api_key) for n in cfg.nodes]
    if a.cmd == "backup":
        m = backup(cfg, s3, clients)
        print(json.dumps({"run": m["run"], "created_at": m["created_at"], "objects": len(m["items"])}))
        return 0
    if a.cmd == "verify":
        result = verify(cfg, s3, a.run)
        print(json.dumps(result))
        return 1 if result["corrupt"] else 0
    n = restore(cfg, s3, clients, a.run, a.collection, a.node)
    print(json.dumps({"run": a.run, "restored": n}))
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())

"""Tests for qdrant_snapshot.py against in-process fakes of S3 and Qdrant."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from qdrant_snapshot import S3, Config, Qdrant, backup, restore, sign_v4, verify


def test_sigv4_matches_the_aws_documented_example() -> None:
    # "Example: GET Object" from the Amazon S3 API reference (Signature Version 4, header auth).
    auth = sign_v4(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/test.txt",
        {},
        {"Range": "bytes=0-9"},
        hashlib.sha256(b"").hexdigest(),
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "us-east-1",
        "20130524T000000Z",
    )
    assert auth == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


class FakeS3(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}
    uploads: dict[str, dict[int, bytes]] = {}

    def log_message(self, *args: object) -> None:
        pass

    def _key(self) -> tuple[str, dict[str, str]]:
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] if v else "" for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        return urllib.parse.unquote(u.path), q

    def _body(self) -> bytes:
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
        assert self.headers["x-amz-content-sha256"] == hashlib.sha256(data).hexdigest()
        return data

    def _reply(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:  # noqa: N802
        key, q = self._key()
        data = self._body()
        if "uploadId" in q:
            self.uploads[q["uploadId"]][int(q["partNumber"])] = data
            self._reply(200, headers={"ETag": f'"{hashlib.md5(data).hexdigest()}"'})  # noqa: S324
        else:
            self.objects[key] = data
            self._reply(200)

    def do_POST(self) -> None:  # noqa: N802
        key, q = self._key()
        data = self._body()
        if "uploads" in q:
            self.uploads["u1"] = {}
            self._reply(200, b"<InitiateMultipartUploadResult><UploadId>u1</UploadId></InitiateMultipartUploadResult>")
        else:
            numbers = [int(n) for n in re.findall(rb"<PartNumber>(\d+)</PartNumber>", data)]
            self.objects[key] = b"".join(self.uploads[q["uploadId"]][n] for n in numbers)
            self._reply(200, b"<CompleteMultipartUploadResult/>")

    def do_GET(self) -> None:  # noqa: N802
        key, _ = self._key()
        self._body()
        if key in self.objects:
            self._reply(200, self.objects[key])
        else:
            self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")


class FakeQdrant(BaseHTTPRequestHandler):
    """One node: two collections whose snapshot bytes name the node."""

    node = "node-0"
    restored: list[tuple[str, str, bytes]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snap(self, coll: str) -> bytes:
        return f"{self.node}:{coll}:".encode() * 4000  # ~60 KB, enough for three 20 KB parts

    def do_GET(self) -> None:  # noqa: N802
        assert self.headers["api-key"] == "qk"
        if self.path == "/collections":
            cols = [{"name": "revenue-authority"}, {"name": "ministry-health"}]
            self._reply(200, json.dumps({"result": {"collections": cols}}).encode())
        else:
            coll = self.path.split("/")[2]
            self._reply(200, self._snap(coll))

    def do_POST(self) -> None:  # noqa: N802
        coll = self.path.split("/")[2]
        if "/snapshots/upload?priority=snapshot" in self.path:
            data = self.rfile.read(int(self.headers["Content-Length"]))
            self.restored.append((self.node, coll, data))
            self._reply(200, b'{"result": true}')
            return
        name = f"{coll}-snap.snapshot"
        body = {"result": {"name": name, "checksum": hashlib.sha256(self._snap(coll)).hexdigest()}}
        self._reply(200, json.dumps(body).encode())

    def do_DELETE(self) -> None:  # noqa: N802
        self._reply(200, b'{"result": true}')


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def s3() -> Iterator[S3]:
    FakeS3.objects, FakeS3.uploads = {}, {}
    gen = _serve(FakeS3)
    client = S3(next(gen), "qdrant", "us-east-1", "AK", "SK", None)
    client.part_size = 20_000
    yield client
    next(gen, None)


@pytest.fixture
def nodes() -> Iterator[list[Qdrant]]:
    FakeQdrant.restored = []
    handlers = [type(f"Node{i}", (FakeQdrant,), {"node": f"node-{i}"}) for i in range(2)]
    gens = [_serve(h) for h in handlers]
    yield [Qdrant(next(g), "qk") for g in gens]
    for g in gens:
        next(g, None)


def cfg(tmp_path: Path) -> Config:
    return Config(nodes=["x", "y"], api_key="qk", prefix="qdrant", workdir=tmp_path / "work")


def test_backup_uploads_every_node_and_writes_manifest_last(s3: S3, nodes: list[Qdrant], tmp_path: Path) -> None:
    m = backup(cfg(tmp_path), s3, nodes, now=datetime(2026, 10, 19, 1, 30, tzinfo=UTC))
    assert m["run"] == "20261019T0130Z" and m["created_at"] == "2026-10-19T01:30:00Z"
    assert [(i["node"], i["collection"]) for i in m["items"]] == [
        (0, "ministry-health"), (0, "revenue-authority"), (1, "ministry-health"), (1, "revenue-authority")
    ]
    key = "/qdrant/qdrant/20261019T0130Z/node-1/ministry-health/ministry-health-snap.snapshot"
    assert FakeS3.objects[key].startswith(b"node-1:ministry-health:")  # reassembled from multipart
    assert FakeS3.objects["/qdrant/qdrant/LATEST"] == b"20261019T0130Z"
    assert list((tmp_path / "work").iterdir()) == []  # scratch space is emptied


def test_verify_detects_a_corrupted_object(s3: S3, nodes: list[Qdrant], tmp_path: Path) -> None:
    backup(cfg(tmp_path), s3, nodes, now=datetime(2026, 10, 19, 1, 30, tzinfo=UTC))
    assert verify(cfg(tmp_path), s3, None) == {
        "run": "20261019T0130Z", "created_at": "2026-10-19T01:30:00Z", "verified": 4, "corrupt": []
    }
    key = "/qdrant/qdrant/20261019T0130Z/node-0/revenue-authority/revenue-authority-snap.snapshot"
    FakeS3.objects[key] = b"bit rot"
    assert verify(cfg(tmp_path), s3, "20261019T0130Z")["corrupt"] == [key.removeprefix("/qdrant/")]


def test_restore_sends_each_snapshot_back_to_its_own_node(s3: S3, nodes: list[Qdrant], tmp_path: Path) -> None:
    backup(cfg(tmp_path), s3, nodes, now=datetime(2026, 10, 19, 1, 30, tzinfo=UTC))
    assert restore(cfg(tmp_path), s3, nodes, "20261019T0130Z", "ministry-health", None) == 2
    assert [(n, c) for n, c, _ in FakeQdrant.restored] == [("node-0", "ministry-health"), ("node-1", "ministry-health")]
    _, _, body = FakeQdrant.restored[1]
    assert b'name="snapshot"' in body and b"node-1:ministry-health:" in body


def test_restore_refuses_a_different_cluster_shape(s3: S3, nodes: list[Qdrant], tmp_path: Path) -> None:
    backup(cfg(tmp_path), s3, nodes, now=datetime(2026, 10, 19, 1, 30, tzinfo=UTC))
    with pytest.raises(SystemExit, match="2 nodes"):
        restore(cfg(tmp_path), s3, nodes[:1], "20261019T0130Z", None, None)

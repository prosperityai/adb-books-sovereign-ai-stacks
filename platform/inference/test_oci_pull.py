"""Tests for oci_pull.py.

Unit tests run anywhere. The integration test needs a throwaway registry:
    registry serve config.yml   # distribution v3 on 127.0.0.1:5000
    OCI_TEST_REF=127.0.0.1:5000/models/tiny-bert-onnx:f171d7baecaf pytest platform/inference
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from oci_pull import (
    MARKER,
    credentials_from_docker_config,
    parse_reference,
    pull,
    safe_relative_path,
    verify_manifest_file,
)


def test_parse_tag_and_digest() -> None:
    r = parse_reference("harbor.enclave.internal/models/qwen3.8-27b-fp8:017b9c7af6b5")
    assert (r.registry, r.repository, r.reference) == (
        "harbor.enclave.internal",
        "models/qwen3.8-27b-fp8",
        "017b9c7af6b5",
    )
    d = "sha256:" + "a" * 64
    r = parse_reference(f"127.0.0.1:5000/models/x@{d}")
    assert r.registry == "127.0.0.1:5000" and r.is_digest and r.reference == d


def test_refuses_implicit_latest_and_bad_digest() -> None:
    with pytest.raises(ValueError, match="latest"):
        parse_reference("harbor.enclave.internal/models/qwen")
    with pytest.raises(ValueError, match="invalid digest"):
        parse_reference("harbor.enclave.internal/models/qwen@sha256:abc")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../root/.ssh/id_rsa", "a/../../b", ""])
def test_rejects_path_traversal(bad: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(bad)


def test_docker_config_both_forms(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    auth = base64.b64encode(b"robot$models-pull:s3cr3t").decode()
    cfg.write_text(json.dumps({"auths": {"harbor.enclave.internal": {"auth": auth}}}))
    assert credentials_from_docker_config(cfg, "harbor.enclave.internal") == ("robot$models-pull", "s3cr3t")
    cfg.write_text(json.dumps({"auths": {"https://h": {"username": "u", "password": "p"}}}))
    assert credentials_from_docker_config(cfg, "h") == ("u", "p")
    assert credentials_from_docker_config(cfg, "other") is None


def test_manifest_file_detects_tampering(tmp_path: Path) -> None:
    (tmp_path / "w.bin").write_bytes(b"weights")
    import hashlib

    digest = hashlib.sha256(b"weights").hexdigest()
    (tmp_path / "MANIFEST.sha256").write_text(f"{digest}  w.bin\n")
    assert verify_manifest_file(tmp_path) == 1
    (tmp_path / "w.bin").write_bytes(b"weightz")
    with pytest.raises(ValueError, match="mismatch"):
        verify_manifest_file(tmp_path)


@pytest.mark.skipif("OCI_TEST_REF" not in os.environ, reason="needs a test registry")
def test_integration_pull_and_cache_hit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "model"
    digest = pull(os.environ["OCI_TEST_REF"], out, plain_http=True)
    assert (out / MARKER).read_text().strip() == digest
    assert (out / "MANIFEST.sha256").exists()
    assert verify_manifest_file(out) > 0
    # Second pull of the same digest is a no-op.
    assert pull(os.environ["OCI_TEST_REF"], out, plain_http=True) == digest
    assert "cache hit" in capsys.readouterr().out
    # Pinning to the wrong digest fails before any download.
    with pytest.raises(ValueError, match="expected"):
        pull(os.environ["OCI_TEST_REF"], tmp_path / "other", plain_http=True, expect_digest="sha256:" + "0" * 64)

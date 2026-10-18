"""Token validation against a real JWKS endpoint served in-process."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from rag.auth import AuthError, TokenVerifier

ISSUER = "https://sso.enclave.internal/realms/government"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks_url():
    jwk = json.loads(RSAAlgorithm.to_jwk(KEY.public_key()))
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    body = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/certs"
    server.shutdown()


def token(key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": ["rag-api"],
        "sub": "alice",
        "azp": "rag-ministry-health",
        "iat": now,
        "exp": now + 300,
        "tenant": "ministry-health",
        "clearance": "confidential",
        "groups": ["/ministry-health", "/hr-directorate"],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def test_valid_token(jwks_url: str) -> None:
    p = TokenVerifier(ISSUER, jwks_url, "rag-api").verify(token())
    assert (p.tenant, p.clearance, p.groups) == ("ministry-health", 2, ("hr-directorate", "ministry-health"))


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"aud": ["chat-revenue-authority"]}, "audience"),
        ({"iss": "https://evil.example/realms/government"}, "issuer"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"tenant": None}, "tenant"),
        ({"clearance": "top-secret"}, "clearance"),
    ],
)
def test_rejected_tokens(jwks_url: str, overrides: dict, reason: str) -> None:
    with pytest.raises(AuthError):
        TokenVerifier(ISSUER, jwks_url, "rag-api").verify(token(**overrides))


def test_token_signed_by_another_key_is_rejected(jwks_url: str) -> None:
    with pytest.raises(AuthError):
        TokenVerifier(ISSUER, jwks_url, "rag-api").verify(token(key=OTHER_KEY))

"""Token validation. The tenant, clearance and groups come ONLY from a signed token.

Book: Sovereign AI Stacks, Chapters 7 and 8
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

# Clearance levels in ascending order. Documents carry one; users carry one.
CLEARANCE_LEVELS = ("public", "restricted", "confidential", "secret")


class AuthError(Exception):
    """Raised for any token that must not be trusted."""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    clearance: int
    groups: tuple[str, ...]
    client_id: str

    @property
    def acl_scope(self) -> tuple[str, int, tuple[str, ...]]:
        """Everything that changes what this principal may see (used as a cache key)."""
        return (self.tenant, self.clearance, self.groups)


def clearance_rank(label: str) -> int:
    try:
        return CLEARANCE_LEVELS.index(label)
    except ValueError:
        raise AuthError(f"unknown clearance {label!r}") from None


class TokenVerifier:
    """Validates Keycloak access tokens: signature, issuer, audience, expiry."""

    def __init__(self, issuer: str, jwks_url: str, audience: str, ca_file: str | None = None) -> None:
        self.issuer = issuer
        self.audience = audience
        ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self.jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=300, ssl_context=ctx)

    def verify(self, token: str) -> Principal:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,  # the token must be FOR this service
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=30,
            )
        except jwt.PyJWTError as err:
            raise AuthError(str(err)) from None
        return principal_from_claims(claims)


def principal_from_claims(claims: dict) -> Principal:
    tenant = claims.get("tenant")
    if not isinstance(tenant, str) or not tenant:
        raise AuthError("token has no tenant claim")
    clearance = clearance_rank(str(claims.get("clearance", "public")))
    groups = tuple(sorted(str(g).lstrip("/") for g in claims.get("groups", []) or []))
    return Principal(
        subject=str(claims["sub"]),
        tenant=tenant,
        clearance=clearance,
        groups=groups,
        client_id=str(claims.get("azp", "")),
    )

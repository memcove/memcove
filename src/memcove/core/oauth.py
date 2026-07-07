"""Native OAuth 2.1 resource-server support.

Memcove acts as an MCP *resource server*: it validates bearer JWTs minted by an
external OIDC authorization server (the IdP — Keycloak by default, but any compliant
provider works) against that IdP's JWKS. The authorization dance (login, consent,
dynamic client registration) is the IdP's job; Memcove only verifies the resulting
token and maps it to a tenant.

``build_token_verifier`` returns an object satisfying the MCP SDK's ``TokenVerifier``
protocol, wired into ``FastMCP(token_verifier=...)`` in ``server.py`` when
``MEMCOVE_OAUTH_ENABLED`` is set.
"""

from __future__ import annotations

import json
import logging
import urllib.request

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

from memcove.core.config import Settings

logger = logging.getLogger("memcove.oauth")


def _discover_jwks_uri(issuer: str) -> str:
    """Fetch the IdP's OIDC discovery document and return its ``jwks_uri``."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - operator-configured issuer
        doc = json.loads(resp.read().decode("utf-8"))
    jwks_uri = doc.get("jwks_uri")
    if not jwks_uri:
        raise ValueError(f"OIDC discovery at {url} has no jwks_uri")
    return jwks_uri


def _scopes_from_claims(claims: dict) -> list[str]:
    """OAuth scopes live in `scope` (space-delimited) or `scp` (list), per provider."""
    scope = claims.get("scope")
    if isinstance(scope, str):
        return scope.split()
    scp = claims.get("scp")
    if isinstance(scp, list):
        return scp
    return []


class JWKSTokenVerifier:
    """Verifies bearer JWTs against an IdP's JWKS (signature, issuer, audience, expiry)."""

    def __init__(
        self,
        *,
        jwks_uri: str,
        issuer: str,
        audience: str,
        required_scopes: list[str],
        algorithms: list[str],
    ) -> None:
        self._issuer = issuer
        self._audience = audience or None
        self._required = set(required_scopes)
        self._algorithms = algorithms
        # PyJWKClient caches keys and refetches on rotation.
        self._jwk_client = PyJWKClient(jwks_uri)

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer or None,
                options={"verify_aud": self._audience is not None},
            )
        except Exception as exc:  # noqa: BLE001 - any failure = not authenticated
            logger.info("token rejected: %s", exc)
            return None

        scopes = _scopes_from_claims(claims)
        if self._required and not self._required.issubset(scopes):
            logger.info("token missing required scopes %s", self._required - set(scopes))
            return None

        return AccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("client_id") or claims.get("sub") or "",
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=claims.get("sub"),
            claims=claims,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # PyJWKClient does blocking network I/O; keep it off the event loop.
        return await anyio.to_thread.run_sync(self._verify_sync, token)


def build_token_verifier(settings: Settings) -> JWKSTokenVerifier:
    """Construct the JWKS verifier from settings, deriving the JWKS URI if needed."""
    if not settings.oauth_issuer:
        raise ValueError("MEMCOVE_OAUTH_ISSUER is required when OAuth is enabled")
    jwks_uri = settings.oauth_jwks_uri or _discover_jwks_uri(settings.oauth_issuer)
    return JWKSTokenVerifier(
        jwks_uri=jwks_uri,
        issuer=settings.oauth_issuer,
        audience=settings.oauth_audience,
        required_scopes=settings.oauth_required_scopes,
        algorithms=settings.oauth_algorithms,
    )

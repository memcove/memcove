"""Unit tests for native OAuth resource-server support (no live IdP required).

We generate a throwaway RSA key, sign JWTs with it, and stub the verifier's JWKS
client to return the matching public key — so these exercise the real PyJWT decode
path (signature, issuer, audience, expiry, scopes) without network I/O.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from memcove.core import tenancy
from memcove.core.config import get_settings
from memcove.core.errors import TenancyError
from memcove.core.oauth import JWKSTokenVerifier, _scopes_from_claims

ISSUER = "https://idp.example.com/realms/memcove"
AUDIENCE = "memcove"


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _sign(priv, **claims) -> str:
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "exp": int(time.time()) + 300}
    payload.update(claims)
    return pyjwt.encode(payload, priv, algorithm="RS256")


def _verifier(pub, monkeypatch, *, required_scopes=None, audience=AUDIENCE):
    v = JWKSTokenVerifier(
        jwks_uri="http://unused",
        issuer=ISSUER,
        audience=audience,
        required_scopes=required_scopes or [],
        algorithms=["RS256"],
    )

    class _Key:
        key = pub

    monkeypatch.setattr(v._jwk_client, "get_signing_key_from_jwt", lambda token: _Key())
    return v


async def test_valid_token_returns_access_token(keypair, monkeypatch):
    priv, pub = keypair
    v = _verifier(pub, monkeypatch, required_scopes=["memcove.use"])
    token = _sign(priv, scope="memcove.use extra", sub="alice")
    at = await v.verify_token(token)
    assert at is not None
    assert at.subject == "alice"
    assert "memcove.use" in at.scopes
    assert at.claims["iss"] == ISSUER


async def test_bad_signature_rejected(keypair, monkeypatch):
    _priv, pub = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = _verifier(pub, monkeypatch)
    token = _sign(other, scope="memcove.use")  # signed by a different key
    assert await v.verify_token(token) is None


async def test_wrong_issuer_rejected(keypair, monkeypatch):
    priv, pub = keypair
    v = _verifier(pub, monkeypatch)
    assert await v.verify_token(_sign(priv, iss="https://evil.example")) is None


async def test_wrong_audience_rejected(keypair, monkeypatch):
    priv, pub = keypair
    v = _verifier(pub, monkeypatch)
    assert await v.verify_token(_sign(priv, aud="someone-else")) is None


async def test_expired_token_rejected(keypair, monkeypatch):
    priv, pub = keypair
    v = _verifier(pub, monkeypatch)
    assert await v.verify_token(_sign(priv, exp=int(time.time()) - 10)) is None


async def test_missing_required_scope_rejected(keypair, monkeypatch):
    priv, pub = keypair
    v = _verifier(pub, monkeypatch, required_scopes=["memcove.use"])
    assert await v.verify_token(_sign(priv, scope="something.else")) is None


async def test_audience_check_skipped_when_unset(keypair, monkeypatch):
    priv, pub = keypair
    v = _verifier(pub, monkeypatch, audience="")  # no audience configured
    # A token with any aud must still verify when we don't pin one.
    assert await v.verify_token(_sign(priv, aud="whatever")) is not None


def test_scopes_from_claims_both_shapes():
    assert _scopes_from_claims({"scope": "a b c"}) == ["a", "b", "c"]
    assert _scopes_from_claims({"scp": ["a", "b"]}) == ["a", "b"]
    assert _scopes_from_claims({}) == []


# ------------------------------------------------------ tenant-from-claims mapping


def test_tenant_from_claims_uses_claim_without_map(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MEMCOVE_OAUTH_TENANT_CLAIM", "preferred_username")
    try:
        assert tenancy.resolve_tenant_from_claims({"preferred_username": "Acme"}) == "t_acme"
    finally:
        get_settings.cache_clear()


def test_tenant_from_claims_map_wins_and_is_fail_closed(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MEMCOVE_TENANT_MAP", '{"alice":"acme","eng":"platform"}')
    try:
        assert tenancy.resolve_tenant_from_claims({"sub": "alice"}) == "t_acme"
        # group/role match when subject isn't mapped
        assert tenancy.resolve_tenant_from_claims({"sub": "bob", "groups": ["eng"]}) == "t_platform"
        # unmapped identity is rejected, never falls through to a raw claim
        with pytest.raises(TenancyError):
            tenancy.resolve_tenant_from_claims({"sub": "nobody"})
    finally:
        get_settings.cache_clear()


def test_tenant_from_claims_missing_claim_rejected(monkeypatch):
    get_settings.cache_clear()
    try:
        with pytest.raises(TenancyError):
            tenancy.resolve_tenant_from_claims({"email": "x@y.z"})  # no `sub`
    finally:
        get_settings.cache_clear()


# ------------------------------------------------ resource cross-tenant enforcement


class _Ctx:
    def __init__(self, headers):
        self.request_context = type("RC", (), {"request": type("R", (), {"headers": headers})()})()


def test_resource_rejects_other_tenant(monkeypatch):
    from memcove.server import _authorized_tenant

    get_settings.cache_clear()
    try:
        ctx = _Ctx({"x-memcove-tenant": "acme"})  # header mode; caller is t_acme
        assert _authorized_tenant(ctx, "acme") == "t_acme"
        with pytest.raises(TenancyError):
            _authorized_tenant(ctx, "victim")  # cannot read another tenant's URI
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------ end-to-end middleware wiring


def test_unauthenticated_request_gets_401_when_oauth_enabled():
    """With OAuth on, the MCP endpoint challenges an anonymous request (RFC 9728)."""
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    class _RejectAll:
        async def verify_token(self, token):
            return None

    app = FastMCP(
        name="memcove",
        token_verifier=_RejectAll(),
        auth=AuthSettings(
            issuer_url=ISSUER,
            resource_server_url="https://memcove.example.com",
            required_scopes=None,
        ),
    ).streamable_http_app()

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 401
    assert "www-authenticate" in {k.lower() for k in resp.headers}

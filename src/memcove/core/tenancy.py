"""Tenant resolution seam.

The single place a caller's identity becomes an internal tenant namespace. Both entry
points — the trusted-proxy-header path (``resolve_tenant``) and the native-OAuth claims
path (``resolve_tenant_from_claims``) — dispatch on ``settings.tenant_mode`` so isolation
is decided by one rule regardless of how the caller authenticated. Everything downstream
keys on the normalized ``t_<id>`` namespace returned here.

Modes (see ``config.py`` for the operator-facing description):

* ``auto``    backward-compatible default — mapped when a map/subject header is set, else
              trust the tenant header (proxy) or the token's tenant claim (OAuth).
* ``shared``  everyone → one tenant (``shared_tenant`` / ``default_tenant``).
* ``private`` every verified identity → its own tenant, derived injectively by hashing the
              identity so distinct users can never collide.
* ``mapped``  explicit ``tenant_map``, fail-closed (unmapped identities rejected).
"""

from __future__ import annotations

import hashlib
import re

from memcove.core.config import get_settings
from memcove.core.errors import TenancyError

# Iceberg namespace component: lowercase alnum + underscore, must start with a letter.
_TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


def normalize_tenant(raw: str | None) -> str:
    """Validate and normalize a raw tenant id into an Iceberg-safe namespace.

    Tenants become a namespace ``t_<id>`` so they never collide with reserved
    catalog namespaces.
    """
    settings = get_settings()
    candidate = (raw or settings.default_tenant).strip().lower()
    candidate = re.sub(r"[^a-z0-9_]", "_", candidate)
    if not re.search(r"[a-z0-9]", candidate):
        raise TenancyError(f"invalid tenant id: {raw!r}")
    namespace = candidate if candidate.startswith("t_") else f"t_{candidate}"
    if not _TENANT_RE.match(namespace):
        raise TenancyError(f"invalid tenant id: {raw!r}")
    return namespace


def _header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    if not name:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _groups(value: str | None) -> list[str]:
    return [g.strip() for g in (value or "").split(",") if g.strip()]


def _shared_tenant(settings) -> str:
    """The single namespace all callers share in ``shared`` mode."""
    return normalize_tenant(settings.shared_tenant or settings.default_tenant)


def _private_tenant(identity: str) -> str:
    """Derive a per-identity namespace that is deterministic *and injective*.

    A raw identity (an OIDC ``sub``, a subject header) may be any string, and merely
    sanitizing it into a namespace is not injective — ``a.b`` and ``a_b`` would both
    normalize to ``t_a_b`` and share data across two different users. Hashing guarantees
    distinct identities map to distinct namespaces, so per-user isolation holds for any
    identity while staying stable across a user's sessions.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return normalize_tenant(f"u{digest}")


def _mapped_tenant(subject: str | None, groups: list[str], settings) -> str:
    """Resolve via ``tenant_map`` (subject first, then a matching group). Fail-closed."""
    if subject and subject in settings.tenant_map:
        return normalize_tenant(settings.tenant_map[subject])
    for group in groups:
        if group in settings.tenant_map:
            return normalize_tenant(settings.tenant_map[group])
    raise TenancyError("caller identity is not provisioned to any tenant")


def resolve_tenant_from_claims(claims: dict) -> str:
    """Resolve the tenant from a *verified* OAuth token's claims (native OAuth mode)."""
    settings = get_settings()
    mode = settings.tenant_mode
    subject = claims.get("sub")
    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]

    if mode == "shared":
        return _shared_tenant(settings)

    if mode == "private":
        identity = claims.get(settings.oauth_tenant_claim) or subject
        if not identity:
            raise TenancyError(
                f"private tenant mode: token has no {settings.oauth_tenant_claim!r} claim"
            )
        return _private_tenant(str(identity))

    if mode == "mapped" or (mode == "auto" and settings.tenant_map):
        return _mapped_tenant(subject, groups, settings)

    # auto, no map: the tenant claim is from a signed token, so it's safe to use directly.
    value = claims.get(settings.oauth_tenant_claim)
    if not value:
        raise TenancyError(f"token has no {settings.oauth_tenant_claim!r} claim for tenant")
    return normalize_tenant(str(value))


def resolve_tenant(headers: dict[str, str] | None) -> str:
    """Resolve the tenant namespace from trusted request headers (proxy mode)."""
    settings = get_settings()
    headers = headers or {}
    mode = settings.tenant_mode

    if mode == "shared":
        return _shared_tenant(settings)

    if mode == "private":
        # A trusted identity is required — the raw, client-settable tenant header is NOT
        # trusted here, so private isolation can't be spoofed by picking a header value.
        subject = _header(headers, settings.tenant_subject_header)
        if not subject:
            raise TenancyError(
                "private tenant mode needs a trusted identity: set tenant_subject_header "
                "so the auth proxy supplies a verified subject"
            )
        return _private_tenant(subject)

    if mode == "mapped" or (mode == "auto" and settings.tenant_subject_header):
        # Fail-closed: the tenant MUST come from the map; never fall through to the raw
        # tenant header — that would let an unmapped caller self-select any tenant.
        subject = _header(headers, settings.tenant_subject_header)
        groups = _groups(_header(headers, settings.tenant_group_header))
        return _mapped_tenant(subject, groups, settings)

    # auto, no subject header: trust the proxy-set tenant header (dev/simple), default when
    # absent. This path is NOT isolated — choose shared/private/mapped for real multi-user.
    return normalize_tenant(_header(headers, settings.tenant_header))

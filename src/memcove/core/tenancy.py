"""Tenant resolution seam.

Memcove sits behind an authenticating proxy that sets trusted headers. This module
turns those headers into a normalized internal tenant namespace — either directly
(``tenant_header``) or via a configurable provisioning map from a verified identity
(subject/group) to an internal id. It is the single place tenant identity is decided;
everything downstream keys on the normalized namespace it returns.
"""

from __future__ import annotations

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


def _map_identity(headers: dict[str, str], settings) -> str | None:
    """Map a proxy-provided identity (subject, else a matching group) to a tenant id."""
    subject = _header(headers, settings.tenant_subject_header)
    if subject and subject in settings.tenant_map:
        return settings.tenant_map[subject]
    groups = _header(headers, settings.tenant_group_header) or ""
    for group in (g.strip() for g in groups.split(",")):
        if group and group in settings.tenant_map:
            return settings.tenant_map[group]
    return None


def resolve_tenant_from_claims(claims: dict) -> str:
    """Resolve the tenant from a *verified* OAuth token's claims (native OAuth mode).

    Same fail-closed provisioning semantics as the header path, but the identity comes
    from a validated JWT rather than a proxy header:

    1. If ``tenant_map`` is configured, map the token's ``sub`` (else a matching group/
       role) through it, and **reject** an unmapped identity — never fall through.
    2. Otherwise use the configured ``oauth_tenant_claim`` value directly (safe because
       it's from a signed token, not client-settable).
    """
    settings = get_settings()
    subject = claims.get("sub")
    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]

    if settings.tenant_map:
        if subject and subject in settings.tenant_map:
            return normalize_tenant(settings.tenant_map[subject])
        for group in groups:
            if group in settings.tenant_map:
                return normalize_tenant(settings.tenant_map[group])
        raise TenancyError("caller identity is not provisioned to any tenant")

    value = claims.get(settings.oauth_tenant_claim)
    if not value:
        raise TenancyError(f"token has no {settings.oauth_tenant_claim!r} claim for tenant")
    return normalize_tenant(str(value))


def resolve_tenant(headers: dict[str, str] | None) -> str:
    """Resolve the tenant namespace from request headers.

    Two modes, in order:

    1. **Provisioning map** — if ``tenant_subject_header`` is configured, map the
       verified identity through ``tenant_map`` to an internal tenant id. This is the
       seam clients wire so a raw OIDC ``sub`` is never used as a namespace directly.
       This mode is fail-closed: an unmapped identity is rejected, never allowed to
       fall through to the client-settable tenant header.
    2. **Direct header** — trust ``tenant_header`` (dev/simple), falling back to the
       default tenant when absent.

    Headers are matched case-insensitively.
    """
    settings = get_settings()
    headers = headers or {}

    if settings.tenant_subject_header:
        # Provisioning mode: the tenant MUST come from the map. Never fall through
        # to the raw, client-settable tenant header — that would let an unmapped
        # caller self-select any tenant, the exact thing this mode exists to stop.
        mapped = _map_identity(headers, settings)
        if mapped is None:
            raise TenancyError("caller identity is not provisioned to any tenant")
        return normalize_tenant(mapped)

    return normalize_tenant(_header(headers, settings.tenant_header))

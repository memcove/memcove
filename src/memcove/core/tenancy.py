"""Tenant resolution seam.

Auth is deferred (see plan). For now the tenant is read from a request header
(``x-memcove-tenant``) and validated/normalized. When real auth lands, only this
module changes: replace ``resolve_tenant`` with token/OAuth introspection that
yields the same normalized tenant id. Everything downstream already keys on it.
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


def resolve_tenant(headers: dict[str, str] | None) -> str:
    """Resolve the tenant namespace from request headers.

    Headers are matched case-insensitively. Falls back to the default tenant
    when the header is absent (single-user / local dev).
    """
    settings = get_settings()
    raw = None
    if headers:
        wanted = settings.tenant_header.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                raw = value
                break
    return normalize_tenant(raw)

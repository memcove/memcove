"""Unit tests for tenant resolution + the provisioning-map seam (no infra)."""

from __future__ import annotations

import pytest

from memcove.core import tenancy
from memcove.core.config import get_settings
from memcove.core.errors import TenancyError


def test_direct_header(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_subject_header", "")
    monkeypatch.setattr(s, "tenant_header", "x-memcove-tenant")
    assert tenancy.resolve_tenant({"X-Memcove-Tenant": "acme"}) == "t_acme"


def test_missing_header_falls_back_to_default(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_subject_header", "")
    monkeypatch.setattr(s, "default_tenant", "default")
    assert tenancy.resolve_tenant({}) == "t_default"


def test_provisioning_map_by_subject(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    monkeypatch.setattr(s, "tenant_map", {"oidc|abc123": "acme"})
    # A raw OIDC subject maps to an internal tenant id, never used as a namespace directly.
    assert tenancy.resolve_tenant({"x-auth-subject": "oidc|abc123"}) == "t_acme"


def test_provisioning_map_by_group(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    monkeypatch.setattr(s, "tenant_group_header", "x-auth-groups")
    monkeypatch.setattr(s, "tenant_map", {"team-research": "research"})
    headers = {"x-auth-subject": "someone", "x-auth-groups": "a, team-research, b"}
    assert tenancy.resolve_tenant(headers) == "t_research"


def test_unmapped_identity_rejected(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    monkeypatch.setattr(s, "tenant_map", {})
    with pytest.raises(TenancyError):
        tenancy.resolve_tenant({"x-auth-subject": "nobody"})


def test_provisioning_mode_never_uses_raw_tenant_header(monkeypatch):
    # Fail-closed: an unmapped identity must NOT fall through to the client-settable
    # tenant header, even when one is present — that would let a caller pick any tenant.
    s = get_settings()
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    monkeypatch.setattr(s, "tenant_map", {})
    monkeypatch.setattr(s, "tenant_header", "x-memcove-tenant")
    headers = {"x-auth-subject": "nobody", "x-memcove-tenant": "acme"}
    with pytest.raises(TenancyError):
        tenancy.resolve_tenant(headers)

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


# --- shared mode: everyone collapses to one tenant --------------------------------------

def test_shared_mode_collapses_every_caller(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "shared")
    monkeypatch.setattr(s, "shared_tenant", "")
    monkeypatch.setattr(s, "default_tenant", "default")
    # header path (with and without a client-chosen tenant) and claims path all agree
    assert tenancy.resolve_tenant({}) == "t_default"
    assert tenancy.resolve_tenant({"x-memcove-tenant": "evil"}) == "t_default"
    assert tenancy.resolve_tenant_from_claims({"sub": "anyone"}) == "t_default"


def test_shared_mode_honours_shared_tenant_setting(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "shared")
    monkeypatch.setattr(s, "shared_tenant", "team-alpha")
    assert tenancy.resolve_tenant({"x-memcove-tenant": "evil"}) == "t_team_alpha"


# --- private mode: every verified identity gets its own isolated tenant ------------------

def test_private_mode_isolates_by_subject(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "private")
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    alice = tenancy.resolve_tenant({"x-auth-subject": "oidc|alice"})
    bob = tenancy.resolve_tenant({"x-auth-subject": "oidc|bob"})
    assert alice.startswith("t_u") and bob.startswith("t_u")
    assert alice != bob  # different users → different namespaces
    # stable across a user's sessions
    assert tenancy.resolve_tenant({"x-auth-subject": "oidc|alice"}) == alice


def test_private_mode_ignores_client_settable_tenant_header(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "private")
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    monkeypatch.setattr(s, "tenant_header", "x-memcove-tenant")
    # the caller tries to select "victim"; private mode derives from the verified subject
    t = tenancy.resolve_tenant({"x-auth-subject": "oidc|alice", "x-memcove-tenant": "victim"})
    assert t != "t_victim"
    assert t == tenancy.resolve_tenant({"x-auth-subject": "oidc|alice"})


def test_private_mode_requires_a_trusted_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "private")
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    with pytest.raises(TenancyError):  # no subject present → refuse, never guess
        tenancy.resolve_tenant({"x-memcove-tenant": "acme"})


def test_private_mode_is_injective_so_isolation_holds(monkeypatch):
    # The isolation guarantee: identities that a naive sanitize would COLLIDE ("a.b" and
    # "a_b" both normalize to t_a_b) must resolve to DIFFERENT namespaces.
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "private")
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    a = tenancy.resolve_tenant({"x-auth-subject": "a.b"})
    b = tenancy.resolve_tenant({"x-auth-subject": "a_b"})
    assert a != b


def test_private_mode_claims_path(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "private")
    monkeypatch.setattr(s, "oauth_tenant_claim", "sub")
    alice = tenancy.resolve_tenant_from_claims({"sub": "alice"})
    bob = tenancy.resolve_tenant_from_claims({"sub": "bob"})
    assert alice.startswith("t_u") and alice != bob
    with pytest.raises(TenancyError):  # no identity claim → refuse
        tenancy.resolve_tenant_from_claims({"email": "x@y.z"})


# --- mapped mode: explicit, fail-closed, regardless of other settings -------------------

def test_mapped_mode_is_explicit_and_fail_closed(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "mapped")
    monkeypatch.setattr(s, "tenant_subject_header", "x-auth-subject")
    monkeypatch.setattr(s, "tenant_map", {"oidc|abc": "acme"})
    assert tenancy.resolve_tenant({"x-auth-subject": "oidc|abc"}) == "t_acme"
    assert tenancy.resolve_tenant_from_claims({"sub": "oidc|abc"}) == "t_acme"
    with pytest.raises(TenancyError):
        tenancy.resolve_tenant({"x-auth-subject": "stranger"})


def test_mapped_mode_empty_map_rejects_everyone(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tenant_mode", "mapped")
    monkeypatch.setattr(s, "tenant_map", {})
    with pytest.raises(TenancyError):
        tenancy.resolve_tenant_from_claims({"sub": "anyone"})

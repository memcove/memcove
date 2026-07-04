"""Unit tests for the configurable Trino principal seam (no infra required)."""

from __future__ import annotations

from memcove.core import trino_client
from memcove.core.config import get_settings


def test_principal_defaults_to_service_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "trino_impersonation", False)
    monkeypatch.setattr(s, "trino_user", "svc")
    # Impersonation off: always the single service identity (local/dev default).
    assert trino_client._principal("t_acme") == "svc"
    assert trino_client._principal(None) == "svc"


def test_principal_impersonates_tenant_when_enabled(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "trino_impersonation", True)
    monkeypatch.setattr(s, "trino_user", "svc")
    # Data requests run AS the tenant so the operator's Trino grants apply per tenant.
    assert trino_client._principal("t_acme") == "t_acme"
    # Provisioning (no run_as) still uses the privileged service identity.
    assert trino_client._principal(None) == "svc"

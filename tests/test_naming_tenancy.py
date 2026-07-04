"""Unit tests for label and tenant normalization (no infra required)."""

from __future__ import annotations

import pytest

from memcove.core.errors import MemcoveError, TenancyError
from memcove.core.naming import validate_label
from memcove.core.tenancy import normalize_tenant, resolve_tenant


def test_validate_label_ok():
    assert validate_label("My_Events") == "my_events"


@pytest.mark.parametrize("bad", ["1events", "no-dash", "drop table", "", "a b"])
def test_validate_label_rejects(bad):
    with pytest.raises(MemcoveError):
        validate_label(bad)


def test_normalize_tenant_prefixes():
    assert normalize_tenant("acme") == "t_acme"
    assert normalize_tenant("t_acme") == "t_acme"
    assert normalize_tenant("Acme Corp") == "t_acme_corp"


def test_normalize_tenant_default():
    assert normalize_tenant(None) == "t_default"


def test_normalize_tenant_rejects_garbage():
    with pytest.raises(TenancyError):
        normalize_tenant("!!!")


def test_resolve_tenant_from_header_case_insensitive():
    assert resolve_tenant({"X-Memcove-Tenant": "acme"}) == "t_acme"
    assert resolve_tenant({"x-memcove-tenant": "beta"}) == "t_beta"
    assert resolve_tenant({}) == "t_default"

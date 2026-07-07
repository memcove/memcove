"""Unit tests for the scratchpad plane (no Trino required).

Cover the mode-aware catalog/schema resolution, the SQL-guard scratch alias, and the
inline Arrow -> Trino VALUES builder. The live DuckDB-behind-Trino path is exercised by
tests/test_integration_scratch.py.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from memcove.core import scratch
from memcove.core.config import get_settings
from memcove.core.sql_guard import SqlGuardError, validate_select
from memcove.tools.ingest import _scratch_select


def _reset():
    get_settings.cache_clear()


# --------------------------------------------------------------- catalog resolution


def test_shared_mode_catalog_and_schema(monkeypatch):
    _reset()
    monkeypatch.setenv("MEMCOVE_SCRATCH_ENABLED", "true")
    monkeypatch.setenv("MEMCOVE_SCRATCH_CATALOG_MODE", "shared")
    monkeypatch.setenv("MEMCOVE_SCRATCH_CATALOG", "scratch")
    try:
        assert scratch.catalog_for("t_acme") == "scratch"
        assert scratch.schema_for("t_acme") == "t_acme"
        assert scratch.qualified("t_acme", "tmp") == '"scratch"."t_acme"."tmp"'
        assert scratch.guard_params("t_acme") == ("scratch", "t_acme")
    finally:
        _reset()


def test_per_tenant_mode_catalog_encodes_tenant(monkeypatch):
    _reset()
    monkeypatch.setenv("MEMCOVE_SCRATCH_ENABLED", "true")
    monkeypatch.setenv("MEMCOVE_SCRATCH_CATALOG_MODE", "per_tenant")
    monkeypatch.setenv("MEMCOVE_SCRATCH_CATALOG_PREFIX", "scratch")
    try:
        assert scratch.catalog_for("t_acme") == "scratch_t_acme"
        assert scratch.catalog_for("t_beta") == "scratch_t_beta"
        # catalog name encodes the tenant -> cross-tenant scratch is a catalog-level split
        assert scratch.guard_params("t_acme") == ("scratch_t_acme", "t_acme")
    finally:
        _reset()


def test_guard_params_none_when_disabled(monkeypatch):
    _reset()
    monkeypatch.setenv("MEMCOVE_SCRATCH_ENABLED", "false")
    try:
        assert scratch.enabled() is False
        assert scratch.guard_params("t_acme") == (None, None)
    finally:
        _reset()


# ---------------------------------------------------------------- guard alias


def test_scratch_alias_rewrites_and_joins():
    g = validate_select(
        "SELECT * FROM scratch.tmp t JOIN mydata d ON t.id = d.id",
        tenant_ns="t_acme", catalog="iceberg", shared_schemas=["ref_market"],
        scratch_catalog="scratch", scratch_schema="t_acme",
    )
    assert "scratch.t_acme.tmp" in g.sql
    assert "iceberg.t_acme.mydata" in g.sql
    assert g.scratch_labels == ["tmp"]
    assert g.referenced_labels == ["mydata"]


def test_scratch_alias_rejected_when_disabled():
    # No scratch_catalog passed -> the alias is just a foreign namespace -> rejected.
    with pytest.raises(SqlGuardError):
        validate_select("SELECT * FROM scratch.tmp", tenant_ns="t_acme", catalog="iceberg")


def test_scratch_catalog_cannot_be_named_directly():
    # An agent must use the `scratch.<label>` alias, never the real catalog name.
    with pytest.raises(SqlGuardError):
        validate_select(
            "SELECT * FROM scratch.t_acme.tmp",  # explicit foreign catalog
            tenant_ns="t_acme", catalog="iceberg",
            scratch_catalog="scratch", scratch_schema="t_acme",
        )


# ---------------------------------------------------------------- VALUES builder


def test_scratch_select_types_and_escaping():
    t = pa.table({"day": ["mon", "o'brien"], "n": [12, 9], "ok": [True, False], "x": [1.5, None]})
    sql = _scratch_select(t)
    assert 'CAST(c0 AS VARCHAR) AS "day"' in sql
    assert 'CAST(c1 AS BIGINT) AS "n"' in sql
    assert 'CAST(c2 AS BOOLEAN) AS "ok"' in sql
    assert 'CAST(c3 AS DOUBLE) AS "x"' in sql
    assert "'o''brien'" in sql  # single-quote escaped
    assert "true" in sql and "false" in sql
    assert "NULL" in sql  # the None cell


def test_scratch_select_empty_table_is_zero_row_shell():
    t = pa.table({"a": pa.array([], type=pa.int64())})
    sql = _scratch_select(t)
    assert "WHERE false" in sql
    assert 'CAST(c0 AS BIGINT) AS "a"' in sql

"""Unit tests for the SQL safety gateway (no infra required)."""

from __future__ import annotations

import pytest

from memcove.core.errors import SqlGuardError
from memcove.core.sql_guard import validate_select, wrap_preview

TENANT = "t_acme"
CAT = "iceberg"


def test_qualifies_bare_label():
    g = validate_select("SELECT * FROM events", TENANT, CAT)
    assert "t_acme" in g.sql and "events" in g.sql and "iceberg" in g.sql
    assert g.referenced_labels == ["events"]


def test_qualifies_join_of_two_objects():
    g = validate_select(
        "SELECT * FROM a JOIN b ON a.id = b.id", TENANT, CAT
    )
    assert set(g.referenced_labels) == {"a", "b"}
    assert g.sql.count("t_acme") == 2


def test_cte_is_not_treated_as_object():
    g = validate_select(
        "WITH c AS (SELECT * FROM events) SELECT * FROM c", TENANT, CAT
    )
    assert g.referenced_labels == ["events"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE events",
        "INSERT INTO events VALUES (1)",
        "UPDATE events SET x = 1",
        "DELETE FROM events",
        "CREATE TABLE x AS SELECT 1",
        "ALTER TABLE events ADD COLUMN y int",
    ],
)
def test_rejects_non_read_statements(sql):
    with pytest.raises(SqlGuardError):
        validate_select(sql, TENANT, CAT)


def test_rejects_multiple_statements():
    with pytest.raises(SqlGuardError):
        validate_select("SELECT 1; SELECT 2", TENANT, CAT)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM analytics.events",
        "SELECT * FROM other_tenant.secrets",
        "SELECT * FROM iceberg.information_schema.tables",
        "SELECT * FROM system.runtime.nodes",
    ],
)
def test_rejects_cross_namespace_and_catalog(sql):
    with pytest.raises(SqlGuardError):
        validate_select(sql, TENANT, CAT)


def test_allows_own_namespace_qualified():
    g = validate_select("SELECT * FROM t_acme.events", TENANT, CAT)
    assert g.referenced_labels == ["events"]


# --------------------------------------------------------------- shared reference plane

SHARED = ["ref_market"]


def test_allows_shared_schema_read():
    g = validate_select(
        "SELECT * FROM ref_market.prices", TENANT, CAT, shared_schemas=SHARED
    )
    # A shared schema resolves to ITSELF, never rewritten to the caller's namespace.
    assert "ref_market" in g.sql and "prices" in g.sql
    assert "t_acme" not in g.sql


def test_allows_join_private_scratch_with_shared_plane():
    g = validate_select(
        "SELECT p.px FROM my_scratch s JOIN ref_market.prices p ON s.id = p.id",
        TENANT,
        CAT,
        shared_schemas=SHARED,
    )
    assert "t_acme" in g.sql  # private scratch -> caller namespace
    assert "ref_market" in g.sql  # shared plane -> itself


def test_shared_read_rejected_when_plane_not_configured():
    with pytest.raises(SqlGuardError):
        validate_select("SELECT * FROM ref_market.prices", TENANT, CAT)


def test_write_into_shared_plane_rejected():
    # Writes never reach the guard as SQL (SELECT-only); a CTAS is rejected regardless.
    with pytest.raises(SqlGuardError):
        validate_select(
            "CREATE TABLE ref_market.x AS SELECT 1", TENANT, CAT, shared_schemas=SHARED
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t_other.secrets",  # cross-tenant read
        'SELECT * FROM "T_Other".secrets',  # quoted mixed-case must not bypass
        "SELECT * FROM information_schema.tables",  # metadata enumeration
        "SELECT * FROM iceberg.information_schema.columns",
        "SELECT * FROM system.jdbc.tables",  # foreign catalog metadata
        "SHOW SCHEMAS",  # enumeration verb
        "SHOW TABLES FROM t_other",
        "DESCRIBE t_other.secrets",
        # Polymorphic table function: parses as an empty-name table; fail closed.
        "SELECT * FROM TABLE(system.query(query => 'select * from t_other.y'))",
    ],
)
def test_rejects_cross_tenant_and_metadata_even_with_shared_plane(sql):
    with pytest.raises(SqlGuardError):
        validate_select(sql, TENANT, CAT, shared_schemas=SHARED)


def test_wrap_preview_injects_limit():
    g = validate_select("SELECT * FROM events", TENANT, CAT)
    wrapped = wrap_preview(g.sql, 100)
    assert "LIMIT 101" in wrapped  # cap + 1 to detect truncation


def test_wrap_preview_preserves_order_by():
    g = validate_select("SELECT * FROM events ORDER BY ts", TENANT, CAT)
    up = wrap_preview(g.sql, 50).upper()
    assert "ORDER BY" in up and "LIMIT 51" in up
    # ORDER BY must apply to the result, i.e. come before the LIMIT
    assert up.index("ORDER BY") < up.index("LIMIT")


def test_wrap_preview_respects_smaller_user_limit():
    g = validate_select("SELECT * FROM events LIMIT 5", TENANT, CAT)
    assert "LIMIT 5" in wrap_preview(g.sql, 1000).upper()

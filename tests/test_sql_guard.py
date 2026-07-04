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

"""Unit tests for reference-plane discovery (Trino calls mocked, no infra)."""

from __future__ import annotations

from memcove.core import trino_client
from memcove.core.config import get_settings
from memcove.tools import discovery


def test_discovery_groups_columns_by_table(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "shared_schemas", ["ref_market"])

    def fake_execute(sql, run_as=None):
        assert "ref_market" in sql  # scoped to the shared schema
        assert run_as is None  # service principal, not a tenant
        return (
            ["table_name", "column_name", "data_type"],
            [
                ["prices", "sym", "varchar"],
                ["prices", "px", "double"],
                ["fx", "pair", "varchar"],
            ],
        )

    monkeypatch.setattr(trino_client, "execute", fake_execute)
    out = discovery.discover_reference_data()

    sch = out["schemas"][0]
    assert sch["schema"] == "ref_market"
    tables = {t["name"]: t["columns"] for t in sch["tables"]}
    assert [c["name"] for c in tables["prices"]] == ["sym", "px"]
    assert tables["fx"][0]["type"] == "varchar"


def test_discovery_empty_when_no_shared(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "shared_schemas", [])
    assert discovery.discover_reference_data() == {"schemas": []}


def test_discovery_tolerates_missing_schema(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "shared_schemas", ["ref_missing"])

    def boom(sql, run_as=None):
        raise RuntimeError("schema does not exist")

    monkeypatch.setattr(trino_client, "execute", boom)
    out = discovery.discover_reference_data()
    assert out["schemas"][0] == {"schema": "ref_missing", "tables": []}

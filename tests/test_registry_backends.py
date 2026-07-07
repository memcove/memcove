"""Functional tests of the full registry API across backends.

SQLite runs everywhere (in-memory, no infra). Postgres and MySQL run only when the
compose stack exposes them, and skip otherwise — same pattern as the integration
suite. Each backend is exercised through the public registry.* API so the SQL,
placeholder translation, upsert dialect, tag child-table, and timestamp handling
are all covered per database.
"""

from __future__ import annotations

import socket
from datetime import datetime

import pytest

from memcove.core import registry
from memcove.core.config import get_settings

TENANT = "t_backendtest"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _dsn_for(kind: str) -> str | None:
    if kind == "sqlite":
        return "sqlite://"  # in-memory
    if kind == "postgres":
        return (
            "postgresql://memcove:memcove@localhost:5433/memcove"
            if _reachable("localhost", 5433)
            else None
        )
    if kind == "mysql":
        return (
            "mysql://memcove:memcove@localhost:3306/memcove"
            if _reachable("localhost", 3306)
            else None
        )
    return None


def _wipe():
    b = registry._get()
    with b.connection() as conn:
        for tbl in (
            "memcove_objects",
            "memcove_object_tags",
            "memcove_lineage",
            "memcove_reconcile_tombstone",
        ):
            b.execute(conn, f"DELETE FROM {tbl}")


@pytest.fixture(params=["sqlite", "postgres", "mysql"])
def backend(request, monkeypatch):
    dsn = _dsn_for(request.param)
    if dsn is None:
        pytest.skip(f"{request.param} not reachable")
    monkeypatch.setenv("MEMCOVE_REGISTRY_DSN", dsn)
    get_settings.cache_clear()
    registry.close_pool()
    registry.init_db()
    _wipe()
    yield request.param
    registry.close_pool()
    get_settings.cache_clear()


def test_record_and_get_roundtrip_with_tags(backend):
    registry.record_object(
        TENANT, "people", table_ident="iceberg.t.people", source="inline",
        producing_sql="SELECT 1", tags=["a", "b", "a"],
    )
    obj = registry.get_object(TENANT, "people")
    assert obj is not None
    assert obj["label"] == "people"
    assert obj["source"] == "inline"
    assert sorted(obj["tags"]) == ["a", "b"]  # deduped
    # Timestamps come back as datetimes on every backend (callers rely on .isoformat()).
    assert isinstance(obj["updated_at"], datetime)


def test_upsert_replaces_row_and_tags(backend):
    registry.record_object(TENANT, "d", table_ident="i.d", source="inline", tags=["old"])
    registry.record_object(TENANT, "d", table_ident="i.d2", source="derived", tags=["new"])
    obj = registry.get_object(TENANT, "d")
    assert obj["table_ident"] == "i.d2" and obj["source"] == "derived"
    assert obj["tags"] == ["new"]  # tags fully replaced, not accumulated


def test_list_and_tag_filter(backend):
    registry.record_object(TENANT, "x", table_ident="i.x", source="inline", tags=["red"])
    registry.record_object(TENANT, "y", table_ident="i.y", source="inline", tags=["blue"])
    all_labels = {o["label"] for o in registry.list_objects(TENANT)}
    assert all_labels == {"x", "y"}
    reds = registry.list_objects(TENANT, tags=["red"])
    assert [o["label"] for o in reds] == ["x"]
    assert registry.list_objects(TENANT, tags=["green"]) == []


def test_lineage_roundtrip(backend):
    registry.record_object(TENANT, "child", table_ident="i.child", source="derived")
    registry.set_lineage(TENANT, "child", ["p1", "p2", "child"])  # self-edge dropped
    assert registry.get_parents(TENANT, "child") == ["p1", "p2"]


def test_guarded_write_and_labels(backend):
    ok = registry.record_object_guarded(
        TENANT, "g", table_ident="i.g", source="derived", tags=["t"], lineage_parents=["p"]
    )
    assert ok is True
    assert "g" in registry.labels_for_tenant(TENANT)
    assert registry.get_parents(TENANT, "g") == ["p"]


def test_tombstones(backend):
    registry.bump_tombstone(TENANT, "gone")
    registry.bump_tombstone(TENANT, "gone")
    assert registry.tombstones_for_tenant(TENANT) == {"gone": 2}
    registry.clear_tombstone(TENANT, "gone")
    assert registry.tombstones_for_tenant(TENANT) == {}


def test_delete_removes_row_tags_and_lineage(backend):
    registry.record_object(TENANT, "z", table_ident="i.z", source="inline", tags=["k"])
    registry.set_lineage(TENANT, "z", ["p"])
    registry.delete_object(TENANT, "z")
    assert registry.get_object(TENANT, "z") is None
    assert registry.get_parents(TENANT, "z") == []
    assert registry.list_objects(TENANT, tags=["k"]) == []

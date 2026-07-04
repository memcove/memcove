"""Unit tests for read-repair, the RECONCILED round-trip, and pending_object."""

from __future__ import annotations

from memcove.core.models import MemoryObject, SourceKind
from memcove.tools import objects


def test_sourcekind_reconciled_round_trips():
    # The exact call describe_object makes on a backfilled row: must not raise.
    assert SourceKind("reconciled") is SourceKind.RECONCILED
    obj = MemoryObject(
        tenant="t_acme", label="x", table_ident="iceberg.t_acme.x",
        source=SourceKind("reconciled"),
    )
    assert obj.source is SourceKind.RECONCILED


def test_read_repair_backfills_reconciled_row(monkeypatch):
    recorded = {}
    monkeypatch.setattr(objects.catalog, "table_exists", lambda t, lbl: True)

    def fake_record(t, lbl, *, table_ident, source, **k):
        recorded["source"] = source

    monkeypatch.setattr(objects.registry, "record_object", fake_record)
    monkeypatch.setattr(
        objects.registry, "get_object",
        lambda t, lbl: {"source": "reconciled", "tags": [], "source_ref": None,
                        "producing_sql": None, "created_at": None, "updated_at": None},
    )
    row = objects._read_repair("t_acme", "x")
    assert recorded["source"] == "reconciled"  # backfilled as RECONCILED
    assert row["source"] == "reconciled"


def test_read_repair_returns_none_when_table_absent(monkeypatch):
    monkeypatch.setattr(objects.catalog, "table_exists", lambda t, lbl: False)
    assert objects._read_repair("t_acme", "ghost") is None


def test_pending_object_builds_from_hand_without_registry_read(monkeypatch):
    # pending_object must NOT read the registry (that store just failed).
    def boom(*a, **k):
        raise AssertionError("pending_object must not read the registry")

    monkeypatch.setattr(objects.registry, "get_object", boom)
    monkeypatch.setattr(objects.registry, "get_parents", boom)
    monkeypatch.setattr(objects.catalog, "load_schema", lambda t, lbl: [("id", "long")])
    monkeypatch.setattr(objects, "_row_count", lambda t, lbl: 3)

    obj = objects.pending_object(
        "t_acme", "d", source=SourceKind.DERIVED,
        producing_sql="SELECT 1", parents=["src"], tags=["x"],
    )
    assert obj.metadata_pending is True
    assert obj.source is SourceKind.DERIVED
    assert obj.lineage.parents == ["src"]
    assert obj.lineage.producing_sql == "SELECT 1"
    assert obj.row_count == 3

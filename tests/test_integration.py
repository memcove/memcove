"""End-to-end integration tests. Require the docker-compose stack.

Run with:  docker compose up -d && pytest -m integration
These are skipped automatically if Trino/Postgres are unreachable.
"""

from __future__ import annotations

import socket

import pytest

from memcove.core import catalog, registry
from memcove.core.config import get_settings
from memcove.core.errors import SchemaMismatchError
from memcove.core.tenancy import normalize_tenant
from memcove.tools import derive, ingest, objects, query

pytestmark = pytest.mark.integration

TENANT = normalize_tenant("pytest")


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _stack_up():
    s = get_settings()
    if not _reachable(s.trino_host, s.trino_port):
        pytest.skip("Trino not reachable; bring up docker compose stack")
    registry.init_db()


def _seed():
    ingest.ingest_object(
        TENANT, "people",
        {"kind": "inline", "format": "json_records",
         "records": [{"id": 1, "g": "a"}, {"id": 2, "g": "b"}, {"id": 3, "g": "a"}]},
        mode="replace",
    )


def test_ingest_and_query():
    _seed()
    res = query.run_query(TENANT, "SELECT g, count(*) AS n FROM people GROUP BY g")
    assert set(res.columns) == {"g", "n"}
    assert res.row_count == 2


def test_derive_records_lineage():
    _seed()
    derive.derive_object(
        TENANT, "people_a",
        "SELECT * FROM people WHERE g = 'a'",
        mode="replace",
    )
    meta = objects.describe_object(TENANT, "people_a")
    assert meta.lineage.parents == ["people"]
    assert meta.row_count == 2


def test_cross_tenant_query_blocked():
    other = normalize_tenant("intruder")
    with pytest.raises(Exception):
        query.run_query(other, f'SELECT * FROM "{TENANT}".people')


def test_replace_same_schema_is_atomic_and_preserves_data():
    _seed()  # people: id, g
    ingest.ingest_object(
        TENANT, "people",
        {"kind": "inline", "format": "json_records",
         "records": [{"id": 9, "g": "z"}]},
        mode="replace",
    )
    res = query.run_query(TENANT, "SELECT count(*) AS n FROM people")
    assert res.rows[0][0] == 1  # overwrite replaced, did not append


def test_replace_with_changed_schema_rejected():
    """REGRESSION: drop-then-create silently accepted any new schema; overwrite rejects it."""
    _seed()  # people: id, g
    with pytest.raises(SchemaMismatchError):
        ingest.ingest_object(
            TENANT, "people",
            {"kind": "inline", "format": "json_records",
             "records": [{"id": 1, "g": "a", "extra": "x"}]},  # added column
            mode="replace",
        )
    # The existing object is untouched — still the original 3 rows, original schema.
    cols = {n for n, _ in catalog.load_schema(TENANT, "people")}
    assert cols == {"id", "g"}


def test_derive_replace_rejects_shape_change():
    _seed()
    derive.derive_object(TENANT, "people_a", "SELECT * FROM people WHERE g = 'a'", mode="create")
    with pytest.raises(SchemaMismatchError):
        derive.derive_object(
            TENANT, "people_a", "SELECT id FROM people WHERE g = 'a'", mode="replace"
        )


def test_read_repair_makes_orphaned_object_visible():
    """Simulate a crash between the data write and the registry write."""
    _seed()
    ingest.ingest_object(
        TENANT, "orphan",
        {"kind": "inline", "format": "json_records", "records": [{"id": 1}]},
        mode="replace",
    )
    registry.delete_object(TENANT, "orphan")  # data survives; registry row gone
    meta = objects.describe_object(TENANT, "orphan")  # read-repair backfills inline
    assert meta.source.value == "reconciled"
    assert registry.get_object(TENANT, "orphan") is not None  # row now exists

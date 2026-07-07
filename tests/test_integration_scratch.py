"""End-to-end tests for the DuckDB-behind-Trino scratchpad plane.

Require the docker-compose stack with Trino >= 480 and the `scratch` DuckDB catalog
(infra/trino/etc/catalog/scratch.properties). Run with:

    docker compose up -d && MEMCOVE_SCRATCH_ENABLED=true pytest -m integration -k scratch

Skipped automatically if Trino is unreachable or the scratch catalog isn't loaded.
"""

from __future__ import annotations

import os
import socket

import pytest

from memcove.core import scratch, trino_client
from memcove.core.config import get_settings
from memcove.core.tenancy import normalize_tenant
from memcove.tools import derive, ingest, query

pytestmark = pytest.mark.integration

TENANT = normalize_tenant("scratchtest")
OTHER = normalize_tenant("scratchother")


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _scratch_stack():
    os.environ["MEMCOVE_SCRATCH_ENABLED"] = "true"
    get_settings.cache_clear()
    s = get_settings()
    if not _reachable(s.trino_host, s.trino_port):
        pytest.skip("Trino not reachable; bring up docker compose stack")
    # Skip cleanly on an older Trino / missing DuckDB catalog rather than erroring.
    try:
        trino_client.execute(f'SHOW SCHEMAS FROM "{s.scratch_catalog}"')
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"scratch catalog {s.scratch_catalog!r} unavailable: {exc}")
    yield
    for tenant in (TENANT, OTHER):
        for label in _safe_scratch_labels(tenant):
            try:
                scratch.drop(tenant, label)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
    os.environ.pop("MEMCOVE_SCRATCH_ENABLED", None)
    get_settings.cache_clear()


def _safe_scratch_labels(tenant: str) -> list[str]:
    try:
        return scratch.list_labels(tenant)
    except Exception:  # noqa: BLE001 - schema may not exist yet
        return []


def _seed_lakehouse_people(tenant: str) -> None:
    ingest.ingest_object(
        tenant, "people",
        {"kind": "inline", "format": "json_records", "records": [
            {"id": 1, "name": "ana", "age": 30},
            {"id": 2, "name": "bo", "age": 15},
            {"id": 3, "name": "cy", "age": 40},
        ]},
        mode="replace",
    )


def test_derive_into_scratch_then_join_with_lakehouse():
    _seed_lakehouse_people(TENANT)

    # Derive a small scratch table FROM a lakehouse table (pure Trino SQL, no PyIceberg).
    obj = derive.derive_object(
        TENANT, "adults", "SELECT id, name FROM people WHERE age >= 18",
        mode="replace", target="scratch",
    )
    assert obj.source.value == "scratch"
    assert obj.row_count == 2

    # Query the scratch table on its own via the reserved alias.
    only = query.run_query(TENANT, "SELECT count(*) AS n FROM scratch.adults")
    assert only.rows[0][0] == 2

    # The headline: JOIN scratch (DuckDB) with the lakehouse (Iceberg) in ONE query.
    joined = query.run_query(
        TENANT,
        "SELECT p.name FROM people p JOIN scratch.adults a ON a.id = p.id ORDER BY p.name",
    )
    assert [r[0] for r in joined.rows] == ["ana", "cy"]


def test_remember_inline_into_scratch():
    obj = ingest.ingest_object(
        TENANT, "notes",
        {"kind": "inline", "format": "json_records", "records": [
            {"k": "a", "v": 1}, {"k": "b", "v": 2},
        ]},
        mode="replace", target="scratch",
    )
    assert obj.source.value == "scratch"
    assert obj.row_count == 2
    got = query.run_query(TENANT, "SELECT sum(v) AS s FROM scratch.notes")
    assert got.rows[0][0] == 3


def test_scratch_is_tenant_isolated():
    derive.derive_object(
        TENANT, "secret", "SELECT id FROM people", mode="replace", target="scratch",
    )
    _seed_lakehouse_people(OTHER)
    # OTHER's `scratch.secret` resolves to OTHER's own scratch schema, which has no such
    # table — the guard makes cross-tenant scratch access impossible, never leaking rows.
    with pytest.raises(Exception):  # noqa: B017 - Trino "table not found" for the other tenant
        query.run_query(OTHER, "SELECT count(*) FROM scratch.secret")

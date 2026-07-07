"""Scratchpad plane — DuckDB behind Trino as a federated catalog.

The scratchpad is a fast, small, ephemeral store that lives *behind Trino* as a DuckDB
catalog, so a single guarded Trino query can join scratch datasets with the durable
lakehouse (Iceberg) and the shared reference plane. Agents never see DuckDB directly;
they address scratch via the reserved ``scratch.<label>`` alias in SQL and
``target=scratch`` on remember/derive.

Two operator-selected modes (``MEMCOVE_SCRATCH_CATALOG_MODE``):

* **shared** — one static ``scratch`` DuckDB catalog configured in Trino; tenants are
  isolated by a schema per tenant plus the SQL guard.
* **per_tenant** — a ``scratch_<tenant>`` catalog created on demand via Trino dynamic
  catalog management, each backed by its own DuckDB file (file-level isolation).

This module resolves the per-tenant catalog/schema names and ensures they exist. It is
the only place that knows which mode is active; the guard and tools stay mode-agnostic by
asking for ``catalog_for(tenant)`` / ``schema_for(tenant)``.
"""

from __future__ import annotations

from memcove.core import trino_client
from memcove.core.config import get_settings
from memcove.core.errors import ScratchError
from memcove.core.models import ColumnSchema, MemoryObject, SourceKind
from memcove.core.sql_guard import SCRATCH_ALIAS  # noqa: F401 - re-exported for callers


def enabled() -> bool:
    return get_settings().scratch_enabled


def _require_enabled() -> None:
    if not enabled():
        raise ScratchError(
            "scratchpad is disabled; set MEMCOVE_SCRATCH_ENABLED=true (and configure a "
            "DuckDB scratch catalog in Trino) to use target=scratch / the scratch.<label> alias"
        )


def catalog_for(tenant: str) -> str:
    """The Trino catalog name backing this tenant's scratchpad."""
    s = get_settings()
    if s.scratch_catalog_mode == "per_tenant":
        return f"{s.scratch_catalog_prefix}_{tenant}"
    return s.scratch_catalog


def schema_for(tenant: str) -> str:
    """The schema within the scratch catalog for this tenant.

    The tenant namespace is used in both modes: it isolates tenants within the single
    shared DuckDB file (shared mode) and stays uniform for the guard in per_tenant mode
    (where the catalog already isolates the tenant).
    """
    return tenant


def qualified(tenant: str, label: str) -> str:
    """Fully-qualified, quoted Trino identifier for a scratch table."""
    return f'"{catalog_for(tenant)}"."{schema_for(tenant)}"."{label}"'


def guard_params(tenant: str) -> tuple[str | None, str | None]:
    """(scratch_catalog, scratch_schema) for the SQL guard, or (None, None) when off.

    Passing these to ``validate_select`` lets a query resolve the ``scratch.<label>``
    alias; when scratch is disabled the alias falls through to a cross-namespace reject.
    """
    if not enabled():
        return None, None
    return catalog_for(tenant), schema_for(tenant)


def ensure(tenant: str) -> None:
    """Make the tenant's scratch catalog + schema exist (idempotent).

    shared mode: the catalog is static (operator-configured); we only create the schema.
    per_tenant mode: create the DuckDB-backed catalog via dynamic catalog management,
    then the schema.
    """
    _require_enabled()
    s = get_settings()
    cat = catalog_for(tenant)
    if s.scratch_catalog_mode == "per_tenant":
        url = f"jdbc:duckdb:{s.scratch_duckdb_dir.rstrip('/')}/{tenant}.duckdb"
        # IF NOT EXISTS + service principal: catalog creation is a privileged, one-time op.
        trino_client.execute_update(
            f'CREATE CATALOG IF NOT EXISTS "{cat}" USING duckdb '
            f"WITH (\"connection-url\" = '{url}')"
        )
    elif s.scratch_catalog_mode != "shared":
        raise ScratchError(
            f"unknown MEMCOVE_SCRATCH_CATALOG_MODE {s.scratch_catalog_mode!r}; "
            "expected 'shared' or 'per_tenant'"
        )
    trino_client.execute_update(f'CREATE SCHEMA IF NOT EXISTS "{cat}"."{schema_for(tenant)}"')


def create_as_select(tenant: str, label: str, select_sql: str, *, replace: bool) -> None:
    """Materialize a scratch table from a validated SELECT (CTAS).

    The DuckDB connector doesn't guarantee ``CREATE OR REPLACE TABLE``, so replace is a
    drop-then-create. Scratch is ephemeral, so the (small) non-atomic window is
    acceptable. Runs as the service principal: scratch isolation is enforced by the SQL
    guard + schema, not Trino grants (the DuckDB connector has no per-tenant authz).
    """
    _require_enabled()
    ensure(tenant)
    if replace:
        drop(tenant, label)
    trino_client.execute_update(f"CREATE TABLE {qualified(tenant, label)} AS {select_sql}")


def drop(tenant: str, label: str) -> None:
    """Drop a single scratch dataset."""
    _require_enabled()
    trino_client.execute_update(f"DROP TABLE IF EXISTS {qualified(tenant, label)}")


def describe(tenant: str, label: str) -> MemoryObject:
    """A MemoryObject view of a scratch dataset (columns + row count, from Trino)."""
    _require_enabled()
    ident = qualified(tenant, label)
    _, drows = trino_client.execute(f"DESCRIBE {ident}")
    cols = [ColumnSchema(name=r[0], type=r[1]) for r in drows]
    n = trino_client.scalar(f"SELECT count(*) FROM {ident}")
    return MemoryObject(
        tenant=tenant,
        label=label,
        table_ident=f"{catalog_for(tenant)}.{schema_for(tenant)}.{label}",
        source=SourceKind.SCRATCH,
        schema=cols,
        row_count=int(n) if n is not None else None,
    )


def table_exists(tenant: str, label: str) -> bool:
    # tenant (t_<id>) and label are already validated identifiers, so literal
    # interpolation here is safe (this query is internal, not the agent's SQL).
    _require_enabled()
    cat, schema = catalog_for(tenant), schema_for(tenant)
    _, rows = trino_client.execute(
        f'SELECT table_name FROM "{cat}".information_schema.tables '
        f"WHERE table_schema = '{schema}' AND table_name = '{label}'"
    )
    return len(rows) > 0


def list_labels(tenant: str) -> list[str]:
    """Scratch dataset labels for a tenant (queried live from Trino; not the registry)."""
    _require_enabled()
    cat, schema = catalog_for(tenant), schema_for(tenant)
    _, rows = trino_client.execute(
        f"SELECT table_name FROM \"{cat}\".information_schema.tables "
        f"WHERE table_schema = '{schema}' ORDER BY table_name"
    )
    return [r[0] for r in rows]

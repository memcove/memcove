"""Postgres control-plane registry.

Source of truth for object *metadata* — tags, source, producing SQL, and the
lineage graph. The Iceberg catalog remains the source of truth for the data and
schema; this just adds what the catalog doesn't track conveniently.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from memcove.core.config import get_settings

logger = logging.getLogger("memcove.registry")

_DDL = """
CREATE TABLE IF NOT EXISTS memcove_objects (
    tenant        text        NOT NULL,
    label         text        NOT NULL,
    table_ident   text        NOT NULL,
    source        text        NOT NULL,
    source_ref    text,
    producing_sql text,
    tags          text[]      NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, label)
);

CREATE TABLE IF NOT EXISTS memcove_lineage (
    child_tenant text NOT NULL,
    child_label  text NOT NULL,
    parent_label text NOT NULL,
    PRIMARY KEY (child_tenant, child_label, parent_label)
);

-- Reconciler grace tracking: how many consecutive sweeps a registry row has been
-- absent from the Iceberg catalog. A row is only deleted once it has been absent
-- for reconcile_min_absent_sweeps sweeps (and a final live re-check confirms it).
CREATE TABLE IF NOT EXISTS memcove_reconcile_tombstone (
    tenant       text NOT NULL,
    label        text NOT NULL,
    absent_sweeps int NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant, label)
);
"""


def _conn():
    return psycopg.connect(get_settings().pg_dsn, autocommit=True)


def _conn_tx():
    """A non-autocommit connection: the ``with`` block commits on success, rolls
    back on exception. Used when several statements must land atomically."""
    return psycopg.connect(get_settings().pg_dsn, autocommit=False)


def init_db() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(_DDL)


def _record_object_stmt(
    cur,
    tenant: str,
    label: str,
    table_ident: str,
    source: str,
    source_ref: str | None,
    producing_sql: str | None,
    tags: list[str] | None,
) -> None:
    """Upsert one object row on an open cursor (caller controls the transaction)."""
    cur.execute(
        """
        INSERT INTO memcove_objects
            (tenant, label, table_ident, source, source_ref, producing_sql, tags)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant, label) DO UPDATE SET
            table_ident   = EXCLUDED.table_ident,
            source        = EXCLUDED.source,
            source_ref    = EXCLUDED.source_ref,
            producing_sql = EXCLUDED.producing_sql,
            tags          = EXCLUDED.tags,
            updated_at    = now()
        """,
        (tenant, label, table_ident, source, source_ref, producing_sql, tags or []),
    )


def _set_lineage_stmt(cur, tenant: str, child_label: str, parent_labels: list[str]) -> None:
    """Replace an object's lineage edges on an open cursor (caller controls the txn)."""
    cur.execute(
        "DELETE FROM memcove_lineage WHERE child_tenant = %s AND child_label = %s",
        (tenant, child_label),
    )
    for parent in dict.fromkeys(parent_labels):  # dedupe, keep order
        if parent == child_label:
            continue
        cur.execute(
            """
            INSERT INTO memcove_lineage (child_tenant, child_label, parent_label)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (tenant, child_label, parent),
        )


def record_object(
    tenant: str,
    label: str,
    table_ident: str,
    source: str,
    *,
    source_ref: str | None = None,
    producing_sql: str | None = None,
    tags: list[str] | None = None,
) -> None:
    with _conn() as conn, conn.cursor() as cur:
        _record_object_stmt(
            cur, tenant, label, table_ident, source, source_ref, producing_sql, tags
        )


def set_lineage(tenant: str, child_label: str, parent_labels: list[str]) -> None:
    with _conn() as conn, conn.cursor() as cur:
        _set_lineage_stmt(cur, tenant, child_label, parent_labels)


def record_object_guarded(
    tenant: str,
    label: str,
    table_ident: str,
    source: str,
    *,
    source_ref: str | None = None,
    producing_sql: str | None = None,
    tags: list[str] | None = None,
    lineage_parents: list[str] | None = None,
) -> bool:
    """Record object metadata (and optionally lineage) AFTER the data write.

    The object row and its lineage edges commit in a single transaction, so a
    ``derive`` can never leave a row whose lineage silently failed to write.

    Returns ``True`` on success. On an *infrastructure* failure (the registry is
    unreachable) it does NOT raise: the data is already committed and queryable via
    Trino, so the write itself succeeded. It logs a structured drift signal and
    returns ``False`` — the caller then builds a ``metadata_pending`` response from
    the values it already has, rather than re-reading the registry that just failed.

    Recovery is partial and honest about it: the reconciler / synchronous read-repair
    restore *visibility* by backfilling a ``reconciled`` stub row, so the object is
    listable and queryable again. They do NOT recover this write's lineage, tags, or
    producing_sql — that metadata is lost if the registry never accepts this write.

    Only "registry down" (``OperationalError`` / ``InterfaceError``) is swallowed. A
    logic or data error (bad SQL, wrong param count, constraint violation) is a real
    bug that would otherwise strip metadata off *every* write silently, so it is left
    to raise loudly instead of being disguised as a pending-metadata success.
    """
    try:
        with _conn_tx() as conn, conn.cursor() as cur:
            _record_object_stmt(
                cur, tenant, label, table_ident, source, source_ref, producing_sql, tags
            )
            if lineage_parents is not None:
                _set_lineage_stmt(cur, tenant, label, lineage_parents)
        return True
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # Registry unreachable — data is committed, so don't fail the write.
        logger.warning(
            "registry drift: data for %s.%s committed but the registry is unreachable; "
            "read-repair will restore visibility (a reconciled stub), not this write's "
            "lineage/tags",
            tenant,
            label,
            exc_info=True,
        )
        return False


def get_object(tenant: str, label: str) -> dict[str, Any] | None:
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM memcove_objects WHERE tenant = %s AND label = %s",
            (tenant, label),
        )
        return cur.fetchone()


def list_objects(tenant: str, tags: list[str] | None = None) -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        if tags:
            cur.execute(
                "SELECT * FROM memcove_objects WHERE tenant = %s AND tags && %s "
                "ORDER BY updated_at DESC",
                (tenant, tags),
            )
        else:
            cur.execute(
                "SELECT * FROM memcove_objects WHERE tenant = %s ORDER BY updated_at DESC",
                (tenant,),
            )
        return cur.fetchall()


def labels_for_tenant(tenant: str) -> list[str]:
    """All object labels the registry has for a tenant (reconciler diff input)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT label FROM memcove_objects WHERE tenant = %s", (tenant,))
        return [r[0] for r in cur.fetchall()]


def tombstones_for_tenant(tenant: str) -> dict[str, int]:
    """Map of label -> consecutive-absent-sweep count for a tenant."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT label, absent_sweeps FROM memcove_reconcile_tombstone WHERE tenant = %s",
            (tenant,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def bump_tombstone(tenant: str, label: str) -> None:
    """Record (or increment) that a row was absent from the catalog this sweep."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memcove_reconcile_tombstone (tenant, label, absent_sweeps)
            VALUES (%s, %s, 1)
            ON CONFLICT (tenant, label) DO UPDATE SET
                absent_sweeps = memcove_reconcile_tombstone.absent_sweeps + 1
            """,
            (tenant, label),
        )


def clear_tombstone(tenant: str, label: str) -> None:
    """Drop a row's absent-sweep tracking (it reappeared or was deleted)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memcove_reconcile_tombstone WHERE tenant = %s AND label = %s",
            (tenant, label),
        )


def get_parents(tenant: str, label: str) -> list[str]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT parent_label FROM memcove_lineage "
            "WHERE child_tenant = %s AND child_label = %s ORDER BY parent_label",
            (tenant, label),
        )
        return [r[0] for r in cur.fetchall()]


def delete_object(tenant: str, label: str) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memcove_objects WHERE tenant = %s AND label = %s",
            (tenant, label),
        )
        cur.execute(
            "DELETE FROM memcove_lineage WHERE child_tenant = %s AND child_label = %s",
            (tenant, label),
        )

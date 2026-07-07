"""Control-plane registry — backend-agnostic.

Source of truth for object *metadata* — tags, source, producing SQL, and the
lineage graph. The Iceberg catalog remains the source of truth for the data and
schema; this just adds what the catalog doesn't track conveniently.

The store is pluggable (Postgres, SQLite, or MySQL), selected by the registry DSN
scheme; the per-database differences live in ``registry_backends.py``. Callers use
the functions here and never see the backend.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any

from memcove.core.config import get_settings
from memcove.core.registry_backends import RegistryBackend, make_backend

logger = logging.getLogger("memcove.registry")

_backend: RegistryBackend | None = None
_backend_dsn: str | None = None
_atexit_registered = False


def _get() -> RegistryBackend:
    """Lazily build the process-wide backend for the current DSN.

    Built on first use (not at import) so importing the registry never requires a
    reachable database. Rebuilt if the DSN changes (e.g. between tests).
    """
    global _backend, _backend_dsn, _atexit_registered
    s = get_settings()
    dsn = s.registry_url()
    if _backend is None or _backend_dsn != dsn:
        if _backend is not None:
            _backend.close()
        _backend = make_backend(
            dsn,
            pool_min=s.pg_pool_min_size,
            pool_max=s.pg_pool_max_size,
            pool_timeout=s.pg_pool_timeout,
        )
        _backend_dsn = dsn
        if not _atexit_registered:
            atexit.register(close_pool)
            _atexit_registered = True
    return _backend


def close_pool() -> None:
    """Close and drop the backend (idempotent). For shutdown and test isolation."""
    global _backend, _backend_dsn
    if _backend is not None:
        _backend.close()
        _backend = None
        _backend_dsn = None


def init_db() -> None:
    b = _get()
    with b.connection() as conn:
        for stmt in b.ddl():
            b.execute(conn, stmt)


def ping() -> None:
    """Cheap liveness probe for the registry — raises if the DB is unreachable.

    Used by the server's ``/ready`` endpoint.
    """
    b = _get()
    with b.connection() as conn:
        b.query_one(conn, "SELECT 1")


# ---------------------------------------------------------------- tag helpers


def _replace_tags(b: RegistryBackend, conn, tenant: str, label: str, tags: list[str] | None) -> None:
    b.execute(conn, "DELETE FROM memcove_object_tags WHERE tenant = ? AND label = ?", (tenant, label))
    for tag in dict.fromkeys(tags or []):  # dedupe, preserve order
        b.execute(
            conn,
            "INSERT INTO memcove_object_tags (tenant, label, tag) VALUES (?, ?, ?)",
            (tenant, label, tag),
        )


def _tags_for(b: RegistryBackend, conn, tenant: str, label: str) -> list[str]:
    rows = b.query_all(
        conn,
        "SELECT tag FROM memcove_object_tags WHERE tenant = ? AND label = ? ORDER BY tag",
        (tenant, label),
    )
    return [r["tag"] for r in rows]


# ---------------------------------------------------------------- writes


def _upsert_object(
    b: RegistryBackend,
    conn,
    tenant: str,
    label: str,
    table_ident: str,
    source: str,
    source_ref: str | None,
    producing_sql: str | None,
    tags: list[str] | None,
) -> None:
    b.execute(
        conn,
        b.upsert_object_sql,
        (tenant, label, table_ident, source, source_ref, producing_sql),
    )
    _replace_tags(b, conn, tenant, label, tags)


def _set_lineage(b: RegistryBackend, conn, tenant: str, child_label: str, parents: list[str]) -> None:
    b.execute(
        conn,
        "DELETE FROM memcove_lineage WHERE child_tenant = ? AND child_label = ?",
        (tenant, child_label),
    )
    for parent in dict.fromkeys(parents):  # dedupe, keep order
        if parent == child_label:
            continue
        b.execute(conn, b.insert_lineage_sql, (tenant, child_label, parent))


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
    b = _get()
    with b.connection() as conn:
        _upsert_object(b, conn, tenant, label, table_ident, source, source_ref, producing_sql, tags)


def set_lineage(tenant: str, child_label: str, parent_labels: list[str]) -> None:
    b = _get()
    with b.connection() as conn:
        _set_lineage(b, conn, tenant, child_label, parent_labels)


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

    The object row, its tags, and its lineage edges commit in a single transaction,
    so a ``derive`` can never leave a row whose lineage silently failed to write.

    Returns ``True`` on success. On an *infrastructure* failure (the registry is
    unreachable) it does NOT raise: the data is already committed and queryable via
    Trino, so the write itself succeeded. It logs a structured drift signal and
    returns ``False`` — the caller then builds a ``metadata_pending`` response.

    Only "registry down" (per the backend's ``is_connection_down``) is swallowed. A
    logic or data error (bad SQL, constraint violation) is a real bug that would
    otherwise strip metadata off *every* write silently, so it is left to raise.
    """
    b = _get()
    try:
        with b.connection() as conn:
            _upsert_object(
                b, conn, tenant, label, table_ident, source, source_ref, producing_sql, tags
            )
            if lineage_parents is not None:
                _set_lineage(b, conn, tenant, label, lineage_parents)
        return True
    except Exception as exc:
        if not b.is_connection_down(exc):
            raise
        logger.warning(
            "registry drift: data for %s.%s committed but the registry is unreachable; "
            "read-repair will restore visibility (a reconciled stub), not this write's "
            "lineage/tags",
            tenant,
            label,
            exc_info=True,
        )
        return False


def delete_object(tenant: str, label: str) -> None:
    b = _get()
    with b.connection() as conn:
        b.execute(conn, "DELETE FROM memcove_objects WHERE tenant = ? AND label = ?", (tenant, label))
        b.execute(conn, "DELETE FROM memcove_object_tags WHERE tenant = ? AND label = ?", (tenant, label))
        b.execute(
            conn,
            "DELETE FROM memcove_lineage WHERE child_tenant = ? AND child_label = ?",
            (tenant, label),
        )


# ---------------------------------------------------------------- reads


def get_object(tenant: str, label: str) -> dict[str, Any] | None:
    b = _get()
    with b.connection() as conn:
        row = b.query_one(
            conn, "SELECT * FROM memcove_objects WHERE tenant = ? AND label = ?", (tenant, label)
        )
        if row is not None:
            row["tags"] = _tags_for(b, conn, tenant, label)
        return row


def list_objects(tenant: str, tags: list[str] | None = None) -> list[dict[str, Any]]:
    b = _get()
    with b.connection() as conn:
        if tags:
            placeholders = ", ".join(["?"] * len(tags))
            rows = b.query_all(
                conn,
                "SELECT o.* FROM memcove_objects o WHERE o.tenant = ? AND EXISTS ("
                "SELECT 1 FROM memcove_object_tags t "
                "WHERE t.tenant = o.tenant AND t.label = o.label "
                f"AND t.tag IN ({placeholders})) ORDER BY o.updated_at DESC",
                (tenant, *tags),
            )
        else:
            rows = b.query_all(
                conn,
                "SELECT * FROM memcove_objects WHERE tenant = ? ORDER BY updated_at DESC",
                (tenant,),
            )
        # Attach tags in one pass over the tenant's tag rows.
        tag_rows = b.query_all(
            conn, "SELECT label, tag FROM memcove_object_tags WHERE tenant = ?", (tenant,)
        )
        by_label: dict[str, list[str]] = {}
        for r in tag_rows:
            by_label.setdefault(r["label"], []).append(r["tag"])
        for row in rows:
            row["tags"] = sorted(by_label.get(row["label"], []))
        return rows


def labels_for_tenant(tenant: str) -> list[str]:
    """All object labels the registry has for a tenant (reconciler diff input)."""
    b = _get()
    with b.connection() as conn:
        rows = b.query_all(conn, "SELECT label FROM memcove_objects WHERE tenant = ?", (tenant,))
        return [r["label"] for r in rows]


def get_parents(tenant: str, label: str) -> list[str]:
    b = _get()
    with b.connection() as conn:
        rows = b.query_all(
            conn,
            "SELECT parent_label FROM memcove_lineage "
            "WHERE child_tenant = ? AND child_label = ? ORDER BY parent_label",
            (tenant, label),
        )
        return [r["parent_label"] for r in rows]


# ---------------------------------------------------------------- tombstones


def tombstones_for_tenant(tenant: str) -> dict[str, int]:
    """Map of label -> consecutive-absent-sweep count for a tenant."""
    b = _get()
    with b.connection() as conn:
        rows = b.query_all(
            conn,
            "SELECT label, absent_sweeps FROM memcove_reconcile_tombstone WHERE tenant = ?",
            (tenant,),
        )
        return {r["label"]: r["absent_sweeps"] for r in rows}


def bump_tombstone(tenant: str, label: str) -> None:
    """Record (or increment) that a row was absent from the catalog this sweep."""
    b = _get()
    with b.connection() as conn:
        b.execute(conn, b.upsert_tombstone_sql, (tenant, label))


def clear_tombstone(tenant: str, label: str) -> None:
    """Drop a row's absent-sweep tracking (it reappeared or was deleted)."""
    b = _get()
    with b.connection() as conn:
        b.execute(
            conn,
            "DELETE FROM memcove_reconcile_tombstone WHERE tenant = ? AND label = ?",
            (tenant, label),
        )

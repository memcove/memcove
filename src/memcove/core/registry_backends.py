"""Registry storage backends.

The registry's public API (``core/registry.py``) is backend-agnostic; this module
holds the per-database differences behind a small ``RegistryBackend`` interface,
selected by the DSN scheme:

    postgresql://…  -> PostgresBackend   (pooled psycopg; the production default)
    sqlite://…      -> SQLiteBackend      (embedded; zero-setup local dev)
    mysql://…       -> MySQLBackend       (pymysql; optional extra)

All SQL in the registry is written with ``?`` placeholders and standard
``CURRENT_TIMESTAMP``; each backend translates the placeholder and supplies the
one genuinely dialect-specific statement shape (the upsert) plus its DDL. Rows
come back as plain dicts, with ``created_at`` / ``updated_at`` normalized to
``datetime`` on every backend so callers can treat them uniformly.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------- base

_TS_COLUMNS = ("created_at", "updated_at")


class RegistryBackend:
    """Interface + shared dict/timestamp plumbing for a registry store."""

    #: DBAPI placeholder this driver expects (``?`` or ``%s``).
    placeholder = "?"

    def _q(self, sql: str) -> str:
        """Translate the canonical ``?`` placeholder to the driver's style."""
        return sql if self.placeholder == "?" else sql.replace("?", self.placeholder)

    # --- connection / execution -------------------------------------------

    @contextmanager
    def connection(self):  # pragma: no cover - overridden
        raise NotImplementedError
        yield

    def execute(self, conn, sql: str, params: tuple = ()) -> None:
        cur = conn.cursor()
        try:
            cur.execute(self._q(sql), params)
        finally:
            cur.close()

    def query_all(self, conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = conn.cursor()
        try:
            cur.execute(self._q(sql), params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            cur.close()
        return [self._normalize(r) for r in rows]

    def query_one(self, conn, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query_all(conn, sql, params)
        return rows[0] if rows else None

    def _normalize(self, row: dict[str, Any]) -> dict[str, Any]:
        """Hook for per-backend row fixups. Default: identity."""
        return row

    # --- dialect-specific SQL ---------------------------------------------

    def ddl(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    #: Upsert one object row (6 params: tenant,label,table_ident,source,source_ref,producing_sql).
    upsert_object_sql = ""
    #: Insert a lineage edge, ignoring duplicates (3 params).
    insert_lineage_sql = ""
    #: Insert-or-increment a reconcile tombstone (2 params).
    upsert_tombstone_sql = ""

    def is_connection_down(self, exc: BaseException) -> bool:  # pragma: no cover - overridden
        """Whether an exception means the registry is unreachable (vs a real bug)."""
        return False

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- postgres

# Postgres and SQLite share the exact upsert shape (INSERT … ON CONFLICT … excluded).
_PG_SQLITE_UPSERT_OBJECT = """
INSERT INTO memcove_objects
    (tenant, label, table_ident, source, source_ref, producing_sql)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (tenant, label) DO UPDATE SET
    table_ident   = excluded.table_ident,
    source        = excluded.source,
    source_ref    = excluded.source_ref,
    producing_sql = excluded.producing_sql,
    updated_at    = CURRENT_TIMESTAMP
"""

_PG_SQLITE_INSERT_LINEAGE = """
INSERT INTO memcove_lineage (child_tenant, child_label, parent_label)
VALUES (?, ?, ?)
ON CONFLICT DO NOTHING
"""

_PG_SQLITE_UPSERT_TOMBSTONE = """
INSERT INTO memcove_reconcile_tombstone (tenant, label, absent_sweeps)
VALUES (?, ?, 1)
ON CONFLICT (tenant, label) DO UPDATE SET
    absent_sweeps = memcove_reconcile_tombstone.absent_sweeps + 1
"""


class PostgresBackend(RegistryBackend):
    placeholder = "%s"
    upsert_object_sql = _PG_SQLITE_UPSERT_OBJECT
    insert_lineage_sql = _PG_SQLITE_INSERT_LINEAGE
    upsert_tombstone_sql = _PG_SQLITE_UPSERT_TOMBSTONE

    def __init__(self, dsn: str, *, min_size: int, max_size: int, timeout: float) -> None:
        from psycopg_pool import ConnectionPool

        # Health-check each connection on borrow: after a Postgres restart or an idle
        # drop a pooled socket goes dead, and without this the pool hands out the dead
        # connection — making the guarded write emit a FALSE "registry down" signal.
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            check=ConnectionPool.check_connection,
            name="memcove-registry",
            open=True,
        )

    @contextmanager
    def connection(self):
        # The pool's context commits on clean exit and rolls back on exception.
        with self._pool.connection() as conn:
            yield conn

    def ddl(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS memcove_objects (
                tenant        text        NOT NULL,
                label         text        NOT NULL,
                table_ident   text        NOT NULL,
                source        text        NOT NULL,
                source_ref    text,
                producing_sql text,
                created_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant, label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_object_tags (
                tenant text NOT NULL,
                label  text NOT NULL,
                tag    text NOT NULL,
                PRIMARY KEY (tenant, label, tag)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_lineage (
                child_tenant text NOT NULL,
                child_label  text NOT NULL,
                parent_label text NOT NULL,
                PRIMARY KEY (child_tenant, child_label, parent_label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_reconcile_tombstone (
                tenant        text NOT NULL,
                label         text NOT NULL,
                absent_sweeps int  NOT NULL DEFAULT 1,
                PRIMARY KEY (tenant, label)
            )
            """,
        ]

    def is_connection_down(self, exc: BaseException) -> bool:
        import psycopg

        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    def close(self) -> None:
        self._pool.close()


# ---------------------------------------------------------------- sqlite


class SQLiteBackend(RegistryBackend):
    placeholder = "?"
    upsert_object_sql = _PG_SQLITE_UPSERT_OBJECT
    insert_lineage_sql = _PG_SQLITE_INSERT_LINEAGE
    upsert_tombstone_sql = _PG_SQLITE_UPSERT_TOMBSTONE

    def __init__(self, path: str) -> None:
        # One shared connection guarded by a lock. In-memory DBs cannot be reopened,
        # and the registry's load is low, so serializing is simplest and correct.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def connection(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _normalize(self, row: dict[str, Any]) -> dict[str, Any]:
        # SQLite returns CURRENT_TIMESTAMP as an ISO-ish string; hand callers a
        # datetime like the other drivers do (objects.py calls .isoformat()).
        for col in _TS_COLUMNS:
            val = row.get(col)
            if isinstance(val, str):
                row[col] = datetime.fromisoformat(val)
        return row

    def ddl(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS memcove_objects (
                tenant        text NOT NULL,
                label         text NOT NULL,
                table_ident   text NOT NULL,
                source        text NOT NULL,
                source_ref    text,
                producing_sql text,
                created_at    timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant, label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_object_tags (
                tenant text NOT NULL,
                label  text NOT NULL,
                tag    text NOT NULL,
                PRIMARY KEY (tenant, label, tag)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_lineage (
                child_tenant text NOT NULL,
                child_label  text NOT NULL,
                parent_label text NOT NULL,
                PRIMARY KEY (child_tenant, child_label, parent_label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_reconcile_tombstone (
                tenant        text    NOT NULL,
                label         text    NOT NULL,
                absent_sweeps integer NOT NULL DEFAULT 1,
                PRIMARY KEY (tenant, label)
            )
            """,
        ]

    def is_connection_down(self, exc: BaseException) -> bool:
        # For an embedded file DB, "unreachable" means the file can't be opened or a
        # disk error — not a transient lock or a real logic error.
        if isinstance(exc, sqlite3.OperationalError):
            msg = str(exc).lower()
            return "unable to open" in msg or "disk i/o" in msg
        return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------- mysql


class MySQLBackend(RegistryBackend):
    placeholder = "%s"
    upsert_object_sql = """
    INSERT INTO memcove_objects
        (tenant, label, table_ident, source, source_ref, producing_sql)
    VALUES (?, ?, ?, ?, ?, ?)
    ON DUPLICATE KEY UPDATE
        table_ident   = VALUES(table_ident),
        source        = VALUES(source),
        source_ref    = VALUES(source_ref),
        producing_sql = VALUES(producing_sql),
        updated_at    = CURRENT_TIMESTAMP
    """
    insert_lineage_sql = """
    INSERT IGNORE INTO memcove_lineage (child_tenant, child_label, parent_label)
    VALUES (?, ?, ?)
    """
    upsert_tombstone_sql = """
    INSERT INTO memcove_reconcile_tombstone (tenant, label, absent_sweeps)
    VALUES (?, ?, 1)
    ON DUPLICATE KEY UPDATE absent_sweeps = absent_sweeps + 1
    """

    def __init__(self, dsn: str) -> None:
        import pymysql

        self._pymysql = pymysql
        u = _parse_url(dsn)
        self._params = dict(
            host=u["host"] or "localhost",
            port=u["port"] or 3306,
            user=u["user"] or "root",
            password=u["password"] or "",
            database=(u["path"] or "").lstrip("/") or None,
            autocommit=False,
        )

    @contextmanager
    def connection(self):
        # pymysql connections are not thread-safe; open one per unit of work.
        conn = self._pymysql.connect(**self._params)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ddl(self) -> list[str]:
        # Key columns need a bounded length in MySQL; long SQL goes in TEXT.
        return [
            """
            CREATE TABLE IF NOT EXISTS memcove_objects (
                tenant        varchar(191) NOT NULL,
                label         varchar(191) NOT NULL,
                table_ident   text         NOT NULL,
                source        varchar(64)  NOT NULL,
                source_ref    text,
                producing_sql text,
                created_at    timestamp    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    timestamp    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant, label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_object_tags (
                tenant varchar(191) NOT NULL,
                label  varchar(191) NOT NULL,
                tag    varchar(191) NOT NULL,
                PRIMARY KEY (tenant, label, tag)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_lineage (
                child_tenant varchar(191) NOT NULL,
                child_label  varchar(191) NOT NULL,
                parent_label varchar(191) NOT NULL,
                PRIMARY KEY (child_tenant, child_label, parent_label)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memcove_reconcile_tombstone (
                tenant        varchar(191) NOT NULL,
                label         varchar(191) NOT NULL,
                absent_sweeps int          NOT NULL DEFAULT 1,
                PRIMARY KEY (tenant, label)
            )
            """,
        ]

    def is_connection_down(self, exc: BaseException) -> bool:
        err = self._pymysql.err
        return isinstance(exc, (err.OperationalError, err.InterfaceError))


# ---------------------------------------------------------------- factory


def _parse_url(url: str) -> dict[str, Any]:
    from urllib.parse import urlparse

    p = urlparse(url)
    return {
        "scheme": p.scheme,
        "user": p.username,
        "password": p.password,
        "host": p.hostname,
        "port": p.port,
        "path": p.path,
    }


def _sqlite_path(url: str) -> str:
    rest = url[len("sqlite://") :]
    if rest.startswith("/"):
        rest = rest[1:]
    if rest in ("", ":memory:"):
        return ":memory:"
    return rest


def make_backend(dsn: str, *, pool_min: int, pool_max: int, pool_timeout: float) -> RegistryBackend:
    """Build the backend for a DSN, dispatched by scheme."""
    scheme = dsn.split("://", 1)[0].lower()
    if scheme in ("postgresql", "postgres"):
        return PostgresBackend(dsn, min_size=pool_min, max_size=pool_max, timeout=pool_timeout)
    if scheme == "sqlite":
        return SQLiteBackend(_sqlite_path(dsn))
    if scheme in ("mysql", "mariadb"):
        return MySQLBackend(dsn)
    raise ValueError(
        f"unsupported registry DSN scheme {scheme!r}; expected postgresql, sqlite, or mysql"
    )

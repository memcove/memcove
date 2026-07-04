"""Postgres control-plane registry.

Source of truth for object *metadata* — tags, source, producing SQL, and the
lineage graph. The Iceberg catalog remains the source of truth for the data and
schema; this just adds what the catalog doesn't track conveniently.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from memcove.core.config import get_settings

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
"""


def _conn():
    return psycopg.connect(get_settings().pg_dsn, autocommit=True)


def init_db() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(_DDL)


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


def set_lineage(tenant: str, child_label: str, parent_labels: list[str]) -> None:
    with _conn() as conn, conn.cursor() as cur:
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

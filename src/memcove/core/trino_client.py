"""Trino access — the read / derive / export engine.

All SELECT, CTAS materialization, and artifact generation go through Trino
against the Iceberg catalog. (The write/ingest path uses PyIceberg directly;
see ``catalog.py``.)
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from trino.dbapi import connect

from memcove.core.config import get_settings


def _connect():
    s = get_settings()
    return connect(
        host=s.trino_host,
        port=s.trino_port,
        user=s.trino_user,
        catalog=s.trino_catalog,
        http_scheme="http",
    )


def ensure_schema(namespace: str) -> None:
    """Create the tenant's Iceberg schema if it does not exist."""
    s = get_settings()
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{s.trino_catalog}"."{namespace}"')
        cur.fetchall()


def execute(sql: str) -> tuple[list[str], list[list[Any]]]:
    """Run a query and return (columns, rows)."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, [list(r) for r in rows]


def execute_arrow(sql: str) -> pa.Table:
    """Run a query and return the full result as an Arrow table."""
    columns, rows = execute(sql)
    if not columns:
        return pa.table({})
    cols: dict[str, list[Any]] = {c: [] for c in columns}
    for row in rows:
        for c, v in zip(columns, row):
            cols[c].append(v)
    return pa.table(cols)


def execute_update(sql: str) -> None:
    """Run a DDL/CTAS statement, draining the result so it actually executes."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cur.fetchall()


def scalar(sql: str) -> Any:
    """Run a query expected to return a single value."""
    _, rows = execute(sql)
    if not rows or not rows[0]:
        return None
    return rows[0][0]

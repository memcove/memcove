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


def _principal(run_as: str | None) -> str:
    """The Trino identity to connect as for a request.

    With ``trino_impersonation`` enabled, each request runs under the caller's
    tenant identity, so Trino's OWN access control — whatever grant backend the
    operator configured (file rules, Ranger, OPA, Iceberg REST authz) — applies
    per tenant. This is the defense-in-depth layer beneath the SQL guard: Memcove
    does not implement grants itself, it just connects as the right principal.
    Disabled (the default) keeps the single service identity for local/dev.
    """
    s = get_settings()
    if s.trino_impersonation and run_as:
        return run_as
    return s.trino_user


def _connect(run_as: str | None = None):
    s = get_settings()
    kwargs: dict[str, Any] = dict(
        host=s.trino_host,
        port=s.trino_port,
        user=_principal(run_as),
        catalog=s.trino_catalog,
        http_scheme=s.trino_http_scheme,
    )
    if s.trino_session_properties:
        # Operator-configured resource caps (query_max_run_time, scan bytes, etc.).
        kwargs["session_properties"] = dict(s.trino_session_properties)
    return connect(**kwargs)


def ensure_schema(namespace: str) -> None:
    """Create the tenant's Iceberg schema if it does not exist (provisioning path).

    Runs as the service principal, not the tenant: schema creation is a control-plane
    privilege a tenant identity is not expected to hold.
    """
    s = get_settings()
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{s.trino_catalog}"."{namespace}"')
        cur.fetchall()


def execute(sql: str, run_as: str | None = None) -> tuple[list[str], list[list[Any]]]:
    """Run a query and return (columns, rows)."""
    with _connect(run_as) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, [list(r) for r in rows]


def execute_arrow(sql: str, run_as: str | None = None) -> pa.Table:
    """Run a query and return the full result as an Arrow table."""
    columns, rows = execute(sql, run_as=run_as)
    if not columns:
        return pa.table({})
    cols: dict[str, list[Any]] = {c: [] for c in columns}
    for row in rows:
        for c, v in zip(columns, row):
            cols[c].append(v)
    return pa.table(cols)


def execute_update(sql: str, run_as: str | None = None) -> None:
    """Run a DDL/CTAS statement, draining the result so it actually executes."""
    with _connect(run_as) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cur.fetchall()


def scalar(sql: str, run_as: str | None = None) -> Any:
    """Run a query expected to return a single value."""
    _, rows = execute(sql, run_as=run_as)
    if not rows or not rows[0]:
        return None
    return rows[0][0]

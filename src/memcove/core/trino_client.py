"""Trino access — the read / derive / export engine.

All SELECT, CTAS materialization, and artifact generation go through Trino
against the Iceberg catalog. (The write/ingest path uses PyIceberg directly;
see ``catalog.py``.)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
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


def _arrow_type_from_trino(type_code: str) -> pa.DataType:
    """Map a Trino type name (``cursor.description`` type_code) to an Arrow type.

    The inverse of ``tools.ingest._trino_type``. Scalar types map exactly; complex
    or unrecognized types (``row``/``array``/``map`` and friends) fall back to
    string — the cell is JSON-encoded when the batch is built, so the schema stays
    fixed and every batch is concatenable.
    """
    t = (type_code or "").strip().lower()
    base = re.split(r"[(\s]", t, maxsplit=1)[0]
    simple = {
        "boolean": pa.bool_(),
        "tinyint": pa.int8(),
        "smallint": pa.int16(),
        "integer": pa.int32(),
        "int": pa.int32(),
        "bigint": pa.int64(),
        "real": pa.float32(),
        "double": pa.float64(),
        "date": pa.date32(),
        "varchar": pa.string(),
        "char": pa.string(),
        "varbinary": pa.binary(),
        "json": pa.string(),
        "uuid": pa.string(),
        "time": pa.string(),
        "ipaddress": pa.string(),
    }
    if base in simple:
        return simple[base]
    if base == "decimal":
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", t)
        return pa.decimal128(int(m.group(1)), int(m.group(2))) if m else pa.decimal128(38, 0)
    if base.startswith("timestamp"):
        return pa.timestamp("us")
    return pa.string()


def _build_array(values: list[Any], arrow_type: pa.DataType) -> pa.Array:
    """Build one column's Arrow array against the fixed schema type.

    String-typed columns coerce non-string cells (the complex/unknown fallback)
    to JSON so the array is always string-typed, keeping batches concatenable.
    """
    if pa.types.is_string(arrow_type):
        coerced = [
            v if (v is None or isinstance(v, str)) else json.dumps(v, default=str)
            for v in values
        ]
        return pa.array(coerced, type=pa.string())
    return pa.array(values, type=arrow_type)


def stream_arrow_batches(
    sql: str, run_as: str | None = None, batch_rows: int | None = None
) -> tuple[pa.Schema, Iterator[pa.RecordBatch]]:
    """Run a query and stream the result as Arrow record batches.

    Returns ``(schema, generator)``. The generator pulls ``batch_rows`` at a time
    from the Trino cursor and yields one ``RecordBatch`` per fetch, so peak memory
    is ~one batch rather than the whole result. The schema is fixed from
    ``cursor.description`` up front (not inferred per batch), so null-only or ragged
    chunks stay consistent and the batches concatenate. The connection is held open
    until the generator is exhausted (or closed) and is always closed on exit.
    """
    s = get_settings()
    size = batch_rows or s.stream_batch_rows
    conn = _connect(run_as)
    cur = conn.cursor()
    cur.execute(sql)
    description = cur.description or []
    fields = [(d[0], _arrow_type_from_trino(d[1])) for d in description]
    schema = pa.schema([pa.field(n, t) for n, t in fields])

    if not fields:
        conn.close()

        def _empty() -> Iterator[pa.RecordBatch]:
            return
            yield  # pragma: no cover - marks this a generator

        return schema, _empty()

    def _gen() -> Iterator[pa.RecordBatch]:
        try:
            while True:
                rows = cur.fetchmany(size)
                if not rows:
                    break
                arrays = [
                    _build_array([r[i] for r in rows], t) for i, (_, t) in enumerate(fields)
                ]
                yield pa.record_batch(arrays, schema=schema)
        finally:
            conn.close()

    return schema, _gen()


def execute_arrow(sql: str, run_as: str | None = None) -> pa.Table:
    """Run a query and return the full result as an Arrow table.

    Materializes the entire result; prefer ``stream_arrow_batches`` for large
    results where peak memory matters.
    """
    schema, batches = stream_arrow_batches(sql, run_as=run_as)
    return pa.Table.from_batches(list(batches), schema=schema)


def result_schema(sql: str, run_as: str | None = None) -> pa.Schema:
    """The Arrow schema of a query's result, without fetching any rows.

    Runs the query wrapped in ``LIMIT 0`` so metadata (e.g. Flight ``get_flight_info``)
    is cheap and never materializes the result. Closes its own connection.
    """
    conn = _connect(run_as)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM (\n{sql}\n) AS _s LIMIT 0")
        cur.fetchall()  # drain the (empty) result so the statement completes
        description = cur.description or []
        return pa.schema(
            [pa.field(d[0], _arrow_type_from_trino(d[1])) for d in description]
        )
    finally:
        conn.close()


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

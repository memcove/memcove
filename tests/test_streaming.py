"""Unit tests for the streaming foundation + ingest size guard (no infra).

Covers ``trino_client.stream_arrow_batches`` (fixed-schema, batched, null-safe,
connection-closing) and the s3/upload ingest size guard, using fakes so nothing
touches Trino or S3.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from memcove.core import storage, trino_client
from memcove.core.config import get_settings
from memcove.core.errors import IngestError
from memcove.tools import ingest as ingest_tool


class _FakeCursor:
    def __init__(self, description, rows):
        self._description = description
        self._rows = rows
        self._i = 0
        self.executed = None

    def execute(self, sql):
        self.executed = sql

    @property
    def description(self):
        return self._description

    def fetchmany(self, size):
        out = self._rows[self._i : self._i + size]
        self._i += len(out)
        return out


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _wire(monkeypatch, description, rows):
    conn = _FakeConn(_FakeCursor(description, rows))
    monkeypatch.setattr(trino_client, "_connect", lambda run_as=None: conn)
    return conn


def test_stream_batches_fixed_schema_multibatch_and_nulls(monkeypatch):
    desc = [("id", "bigint"), ("name", "varchar"), ("amt", "double")]
    rows = [[1, "a", 1.5], [2, None, 2.5], [3, "c", None], [4, None, None], [5, "e", 5.5]]
    conn = _wire(monkeypatch, desc, rows)

    schema, batches = trino_client.stream_arrow_batches("SELECT ...", batch_rows=2)
    assert schema.names == ["id", "name", "amt"]
    assert schema.field("id").type == pa.int64()
    assert schema.field("amt").type == pa.float64()

    materialized = list(batches)
    assert [b.num_rows for b in materialized] == [2, 2, 1]  # batch_rows=2 -> 2,2,1
    tbl = pa.Table.from_batches(materialized, schema=schema)
    assert tbl.num_rows == 5
    assert tbl.column("id").to_pylist() == [1, 2, 3, 4, 5]
    assert tbl.column("name").to_pylist() == ["a", None, "c", None, "e"]
    assert conn.closed  # connection closed once the generator is exhausted


def test_stream_empty_result_keeps_schema_and_closes(monkeypatch):
    conn = _wire(monkeypatch, [("id", "bigint")], [])
    schema, batches = trino_client.stream_arrow_batches("SELECT ...")
    tbl = pa.Table.from_batches(list(batches), schema=schema)
    assert tbl.num_rows == 0
    assert schema.names == ["id"]
    assert conn.closed


def test_stream_no_columns_closes_immediately(monkeypatch):
    conn = _wire(monkeypatch, [], [])
    schema, batches = trino_client.stream_arrow_batches("SELECT ...")
    assert list(batches) == []
    assert schema.names == []
    assert conn.closed  # no fields -> connection closed up front


def test_complex_type_falls_back_to_json_string(monkeypatch):
    _wire(monkeypatch, [("m", "map(varchar,bigint)")], [[{"a": 1}], [None]])
    schema, batches = trino_client.stream_arrow_batches("SELECT ...")
    assert pa.types.is_string(schema.field("m").type)
    tbl = pa.Table.from_batches(list(batches), schema=schema)
    assert tbl.column("m").to_pylist() == ['{"a": 1}', None]


def test_execute_arrow_materializes_via_stream(monkeypatch):
    _wire(monkeypatch, [("x", "integer"), ("y", "boolean")], [[1, True], [2, False]])
    tbl = trino_client.execute_arrow("SELECT ...")
    assert tbl.num_rows == 2
    assert tbl.column("x").to_pylist() == [1, 2]
    assert tbl.schema.field("x").type == pa.int32()


@pytest.mark.parametrize(
    "type_code,expected",
    [
        ("bigint", pa.int64()),
        ("integer", pa.int32()),
        ("double", pa.float64()),
        ("real", pa.float32()),
        ("boolean", pa.bool_()),
        ("varchar(10)", pa.string()),
        ("date", pa.date32()),
        ("timestamp(3)", pa.timestamp("us")),
        ("decimal(12,4)", pa.decimal128(12, 4)),
        ("row(a bigint)", pa.string()),
    ],
)
def test_arrow_type_from_trino(type_code, expected):
    assert trino_client._arrow_type_from_trino(type_code) == expected


def test_ingest_size_guard_rejects_oversized_s3_parquet(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_s3_ingest_prefixes", ["s3://mydata"])
    monkeypatch.setattr(s, "ingest_bytes_cap", 100)
    monkeypatch.setattr(storage, "object_size", lambda *a, **k: 101)

    def _must_not_read(*a, **k):
        raise AssertionError("read_parquet_table must not run when over the cap")

    monkeypatch.setattr(storage, "read_parquet_table", _must_not_read)
    with pytest.raises(IngestError, match="over the ingest cap"):
        ingest_tool._table_from_source(
            {"kind": "s3_parquet", "uri": "s3://mydata/f.parquet"}, "t_acme"
        )


def test_ingest_size_guard_allows_within_cap(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_s3_ingest_prefixes", ["s3://mydata"])
    monkeypatch.setattr(s, "ingest_bytes_cap", 10_000)
    monkeypatch.setattr(storage, "object_size", lambda *a, **k: 50)
    monkeypatch.setattr(storage, "read_parquet_table", lambda *a, **k: pa.table({"x": [1]}))
    table, kind, ref = ingest_tool._table_from_source(
        {"kind": "s3_parquet", "uri": "s3://mydata/f.parquet"}, "t_acme"
    )
    assert table.num_rows == 1
    assert ref == "s3://mydata/f.parquet"


def test_ingest_size_guard_applies_to_upload_handle(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ingest_bytes_cap", 100)
    monkeypatch.setattr(storage, "object_size", lambda *a, **k: 500)
    monkeypatch.setattr(storage, "read_parquet_table", lambda *a, **k: pa.table({"x": [1]}))
    # A well-formed handle for this tenant that would otherwise be read.
    handle = "uploads/t_acme/f-abc.parquet"
    with pytest.raises(IngestError, match="over the ingest cap"):
        ingest_tool._table_from_source(
            {"kind": "upload_handle", "handle": handle}, "t_acme"
        )

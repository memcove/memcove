"""Unit tests for streaming export writers (no infra).

Exercises the parquet/csv/json streaming helpers directly with an in-memory
pyarrow buffer standing in for the S3 output stream.
"""

from __future__ import annotations

import orjson
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from memcove.core import storage
from memcove.tools import artifacts


class _KeepOpen:
    """Wrap a BufferOutputStream so the helper's ``with`` block doesn't close it,
    letting the test read ``getvalue()`` afterwards."""

    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *exc):
        return False


def _fake_sink(monkeypatch):
    store: dict = {}

    def _open(bucket, key):
        inner = pa.BufferOutputStream()
        store[(bucket, key)] = inner
        return _KeepOpen(inner)

    monkeypatch.setattr(storage, "open_output_stream", _open)
    return store


_SCHEMA = pa.schema([("id", pa.int64()), ("name", pa.string())])
_BATCHES = [
    pa.record_batch([pa.array([1, 2]), pa.array(["a", "b"])], schema=_SCHEMA),
    pa.record_batch([pa.array([3]), pa.array([None])], schema=_SCHEMA),
]


def _bytes(store, bucket="b", key="k"):
    return store[(bucket, key)].getvalue().to_pybytes()


def test_stream_parquet_roundtrip(monkeypatch):
    store = _fake_sink(monkeypatch)
    rows = artifacts._stream_parquet("b", "k", _SCHEMA, iter(_BATCHES))
    assert rows == 3
    tbl = pq.read_table(pa.BufferReader(_bytes(store)))
    assert tbl.num_rows == 3
    assert tbl.column("id").to_pylist() == [1, 2, 3]
    assert tbl.column("name").to_pylist() == ["a", "b", None]


def test_stream_csv_has_header_and_rows(monkeypatch):
    store = _fake_sink(monkeypatch)
    rows = artifacts._stream_csv("b", "k", _SCHEMA, iter(_BATCHES))
    assert rows == 3
    tbl = pacsv.read_csv(pa.BufferReader(_bytes(store)))
    assert tbl.num_rows == 3
    assert tbl.column("id").to_pylist() == [1, 2, 3]
    assert tbl.column("name").to_pylist()[:2] == ["a", "b"]


def test_stream_json_is_valid_array(monkeypatch):
    store = _fake_sink(monkeypatch)
    rows = artifacts._stream_json("b", "k", iter(_BATCHES))
    assert rows == 3
    data = orjson.loads(_bytes(store))
    assert data == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
        {"id": 3, "name": None},
    ]


def test_stream_parquet_empty_is_valid(monkeypatch):
    store = _fake_sink(monkeypatch)
    rows = artifacts._stream_parquet("b", "k", _SCHEMA, iter([]))
    assert rows == 0
    tbl = pq.read_table(pa.BufferReader(_bytes(store)))
    assert tbl.num_rows == 0
    assert tbl.schema.names == ["id", "name"]


def test_stream_csv_empty_is_header_only(monkeypatch):
    store = _fake_sink(monkeypatch)
    rows = artifacts._stream_csv("b", "k", _SCHEMA, iter([]))
    assert rows == 0
    tbl = pacsv.read_csv(pa.BufferReader(_bytes(store)))
    assert tbl.num_rows == 0
    assert tbl.schema.names == ["id", "name"]


def test_stream_json_empty_is_empty_array(monkeypatch):
    store = _fake_sink(monkeypatch)
    rows = artifacts._stream_json("b", "k", iter([]))
    assert rows == 0
    assert orjson.loads(_bytes(store)) == []

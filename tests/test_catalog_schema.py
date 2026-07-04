"""Unit tests for the write-path schema-compatibility guard (no infra required)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from memcove.core.catalog import _assert_schema_compatible
from memcove.core.errors import SchemaMismatchError


def _schema(fields: dict[str, pa.DataType]) -> pa.Schema:
    return pa.schema([pa.field(n, t) for n, t in fields.items()])


def test_identical_schema_is_compatible():
    s = _schema({"id": pa.int64(), "g": pa.string()})
    # Same names + types (nullability differs) -> compatible, no raise.
    incoming = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("g", pa.string())])
    _assert_schema_compatible(s, incoming, op="replace", label="people")


def test_reordered_columns_are_compatible():
    existing = _schema({"id": pa.int64(), "g": pa.string()})
    incoming = _schema({"g": pa.string(), "id": pa.int64()})
    _assert_schema_compatible(existing, incoming, op="append", label="people")


def test_added_column_rejected():
    existing = _schema({"id": pa.int64()})
    incoming = _schema({"id": pa.int64(), "extra": pa.string()})
    with pytest.raises(SchemaMismatchError) as exc:
        _assert_schema_compatible(existing, incoming, op="replace", label="people")
    assert "added" in str(exc.value)
    assert "forget()" in str(exc.value)


def test_removed_column_rejected():
    existing = _schema({"id": pa.int64(), "g": pa.string()})
    incoming = _schema({"id": pa.int64()})
    with pytest.raises(SchemaMismatchError) as exc:
        _assert_schema_compatible(existing, incoming, op="append", label="people")
    assert "removed" in str(exc.value)


def test_retyped_column_rejected():
    existing = _schema({"id": pa.int64()})
    incoming = _schema({"id": pa.string()})
    with pytest.raises(SchemaMismatchError) as exc:
        _assert_schema_compatible(existing, incoming, op="replace", label="people")
    assert "retyped" in str(exc.value)
    assert "id" in str(exc.value)


def test_large_string_matches_string():
    # REGRESSION: PyIceberg's Schema.as_arrow() returns large_string where the write
    # path produced string. That is an encoding detail, not a schema change — the guard
    # must NOT false-reject a same-shape replace of any object with a string column.
    existing = _schema({"id": pa.int64(), "g": pa.large_string()})  # as read back from Iceberg
    incoming = _schema({"id": pa.int64(), "g": pa.string()})  # as produced by the write path
    _assert_schema_compatible(existing, incoming, op="replace", label="people")


def test_large_list_and_binary_match():
    existing = _schema({"b": pa.large_binary(), "xs": pa.large_list(pa.large_string())})
    incoming = _schema({"b": pa.binary(), "xs": pa.list_(pa.string())})
    _assert_schema_compatible(existing, incoming, op="append", label="blobs")


def test_timestamp_precision_and_int_width_normalized():
    # Iceberg stores timestamps as microseconds and has no int narrower than 32-bit,
    # so as_arrow() reads back timestamp[us]/int32 where the write path produced
    # timestamp[ns]/int16. Same shape -> must not raise.
    existing = _schema({"ts": pa.timestamp("us"), "n": pa.int32()})  # read back from Iceberg
    incoming = _schema({"ts": pa.timestamp("ns"), "n": pa.int16()})  # write-path arrow
    _assert_schema_compatible(existing, incoming, op="replace", label="events")


def test_real_retype_still_rejected_after_normalization():
    # Normalization must not mask a genuine type change.
    existing = _schema({"id": pa.int64()})
    incoming = _schema({"id": pa.string()})
    with pytest.raises(SchemaMismatchError):
        _assert_schema_compatible(existing, incoming, op="replace", label="people")

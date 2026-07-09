"""Unit tests for the hybrid Flight do_put commit path (no infra).

Fakes the Flight reader and the catalog write so we can assert: small uploads
commit once (atomic), over-threshold uploads flush in chunks (first commit uses
the requested mode, the rest append), and an empty upload still creates the table.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.flight as fl

from memcove.core import catalog, registry
from memcove.core.config import get_settings
from memcove.core.tenancy import normalize_tenant
from memcove.data_plane import tickets
from memcove.data_plane.flight_server import MemcoveFlightServer

TENANT = normalize_tenant("pytestput")
_SCHEMA = pa.schema([("id", pa.int64())])


class _Chunk:
    def __init__(self, data):
        self.data = data


class _Reader:
    def __init__(self, batches, schema=_SCHEMA):
        self._batches = batches
        self.schema = schema

    def __iter__(self):
        return (_Chunk(b) for b in self._batches)


def _batch(vals):
    return pa.record_batch([pa.array(vals, type=pa.int64())], schema=_SCHEMA)


def _server():
    return MemcoveFlightServer.__new__(MemcoveFlightServer)


def _record_writes(monkeypatch):
    writes = []

    def _fake_write(tenant, name, table, mode="create"):
        writes.append({"mode": mode, "rows": table.num_rows})
        return table.num_rows

    monkeypatch.setattr(catalog, "write_arrow", _fake_write)
    monkeypatch.setattr(registry, "record_object_guarded", lambda *a, **k: True)
    return writes


def _descriptor(mode):
    return fl.FlightDescriptor.for_command(
        tickets.sign(tickets.ingest_command(TENANT, "ds", mode))
    )


def test_small_upload_is_single_commit(monkeypatch):
    writes = _record_writes(monkeypatch)
    monkeypatch.setattr(get_settings(), "doput_single_commit_max_rows", 1_000_000)
    reader = _Reader([_batch([1, 2]), _batch([3, 4]), _batch([5])])
    _server().do_put(None, _descriptor("create"), reader, None)
    assert writes == [{"mode": "create", "rows": 5}]  # one atomic commit


def test_large_upload_chunks_and_appends(monkeypatch):
    writes = _record_writes(monkeypatch)
    monkeypatch.setattr(get_settings(), "doput_single_commit_max_rows", 2)
    reader = _Reader([_batch([1, 2]), _batch([3, 4]), _batch([5, 6])])
    _server().do_put(None, _descriptor("replace"), reader, None)
    # First flush honors the requested mode; subsequent flushes append.
    assert [w["mode"] for w in writes] == ["replace", "append", "append"]
    assert [w["rows"] for w in writes] == [2, 2, 2]


def test_empty_upload_still_creates_table(monkeypatch):
    writes = _record_writes(monkeypatch)
    monkeypatch.setattr(get_settings(), "doput_single_commit_max_rows", 10)
    _server().do_put(None, _descriptor("create"), _Reader([]), None)
    assert writes == [{"mode": "create", "rows": 0}]  # zero-row table from stream schema


def test_metadata_only_chunks_are_skipped(monkeypatch):
    writes = _record_writes(monkeypatch)
    monkeypatch.setattr(get_settings(), "doput_single_commit_max_rows", 1_000_000)
    reader = _Reader([_batch([1]), None, _batch([2])])  # a None-data chunk in the middle
    _server().do_put(None, _descriptor("create"), reader, None)
    assert writes == [{"mode": "create", "rows": 2}]

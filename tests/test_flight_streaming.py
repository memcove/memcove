"""Unit tests for the streaming Flight do_get / get_flight_info (no infra).

Fakes the Trino layer so we can assert the server streams (GeneratorStream) and
that get_flight_info only describes the schema (LIMIT 0) rather than running the
whole query to count rows.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.flight as fl
import pytest

from memcove.core import trino_client
from memcove.core.tenancy import normalize_tenant
from memcove.data_plane import tickets
from memcove.data_plane.flight_server import MemcoveFlightServer

TENANT = normalize_tenant("pytestflight")
_SCHEMA = pa.schema([("id", pa.int64())])


def _server():
    return MemcoveFlightServer.__new__(MemcoveFlightServer)  # skip binding a socket


def test_validated_query_read_qualifies_to_tenant():
    tenant, sql = _server()._validated_query({"op": "read", "tenant": TENANT, "name": "ds"})
    assert tenant == TENANT
    assert TENANT in sql and "ds" in sql  # bare name rewritten to the tenant namespace


def test_validated_query_rejects_unknown_op():
    with pytest.raises(fl.FlightError):
        _server()._validated_query({"op": "bogus", "tenant": TENANT})


def test_do_get_returns_generator_stream(monkeypatch):
    calls = {}

    def _fake_stream(sql, run_as=None, batch_rows=None):
        calls["sql"] = sql
        calls["run_as"] = run_as
        batch = pa.record_batch([pa.array([1, 2, 3])], schema=_SCHEMA)
        return _SCHEMA, iter([batch])

    monkeypatch.setattr(trino_client, "stream_arrow_batches", _fake_stream)

    ticket = fl.Ticket(tickets.sign(tickets.read_command(TENANT, "ds")))
    result = _server().do_get(None, ticket)

    assert isinstance(result, fl.GeneratorStream)
    assert calls["run_as"] == TENANT
    assert TENANT in calls["sql"]  # streamed the guarded, tenant-qualified sql


def test_get_flight_info_describes_only(monkeypatch):
    def _boom(*a, **k):  # the full query must NOT run here
        raise AssertionError("get_flight_info must not execute the full query")

    monkeypatch.setattr(trino_client, "stream_arrow_batches", _boom)
    monkeypatch.setattr(trino_client, "result_schema", lambda sql, run_as=None: _SCHEMA)

    desc = fl.FlightDescriptor.for_command(tickets.sign(tickets.read_command(TENANT, "ds")))
    info = _server().get_flight_info(None, desc)

    assert info.schema == _SCHEMA
    assert info.total_records == -1  # unknown without executing
    assert info.total_bytes == -1

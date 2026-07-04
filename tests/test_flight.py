"""Integration test for the Arrow Flight data plane. Requires the stack.

Run with:  docker compose up -d && pytest -m integration
Skipped automatically if Trino is unreachable.
"""

from __future__ import annotations

import socket
import threading
import time

import pyarrow as pa
import pyarrow.flight as fl
import pytest

from memcove.core import registry, trino_client
from memcove.core.config import get_settings
from memcove.core.tenancy import normalize_tenant
from memcove.data_plane import tickets
from memcove.data_plane.flight_server import MemcoveFlightServer

pytestmark = pytest.mark.integration

TENANT = normalize_tenant("pytestflight")


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def flight_client():
    s = get_settings()
    if not _reachable(s.trino_host, s.trino_port):
        pytest.skip("Trino not reachable; bring up docker compose stack")
    registry.init_db()
    server = MemcoveFlightServer(f"grpc://0.0.0.0:{s.flight_port}")
    threading.Thread(target=server.serve, daemon=True).start()
    time.sleep(1.0)
    yield fl.connect(s.flight_advertise_uri)
    server.shutdown()


def test_doput_then_doget_roundtrip(flight_client):
    table = pa.table({"id": [1, 2, 3, 4], "g": ["a", "b", "a", "b"]})
    desc = fl.FlightDescriptor.for_command(
        tickets.encode(tickets.ingest_command(TENANT, "fds", "replace"))
    )
    writer, _ = flight_client.do_put(desc, table.schema)
    writer.write_table(table)
    writer.close()

    assert trino_client.scalar(f'SELECT count(*) FROM "iceberg"."{TENANT}"."fds"') == 4

    ticket = fl.Ticket(tickets.encode(tickets.read_command(TENANT, "fds")))
    out = flight_client.do_get(ticket).read_all()
    assert out.num_rows == 4
    assert set(out.column_names) == {"id", "g"}


def test_doget_rejects_cross_tenant(flight_client):
    ticket = fl.Ticket(
        tickets.encode(tickets.query_command(normalize_tenant("intruder"), f'SELECT * FROM "{TENANT}".fds'))
    )
    with pytest.raises(Exception):
        flight_client.do_get(ticket).read_all()

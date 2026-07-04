"""Smoke test for the Arrow Flight streaming data plane (M3).

Starts the Flight server in a background thread, streams a dataset IN via DoPut,
verifies it through Trino, then streams a query result OUT via DoGet.

Requires the docker-compose stack to be up:
    docker compose up -d && python scripts/flight_smoke.py
"""

from __future__ import annotations

import sys
import threading
import time

import pyarrow as pa
import pyarrow.flight as fl

from memcove.core import registry, trino_client
from memcove.core.config import get_settings
from memcove.core.tenancy import normalize_tenant
from memcove.data_plane import tickets
from memcove.data_plane.flight_server import MemcoveFlightServer

TENANT = normalize_tenant("flightsmoke")


def main() -> int:
    registry.init_db()
    settings = get_settings()
    server = MemcoveFlightServer(f"grpc://0.0.0.0:{settings.flight_port}")
    threading.Thread(target=server.serve, daemon=True).start()
    time.sleep(1.0)

    client = fl.connect(settings.flight_advertise_uri)

    print("=== DoPut: stream dataset in ===")
    table = pa.table({"id": list(range(1000)), "v": [i * 2 for i in range(1000)]})
    desc = fl.FlightDescriptor.for_command(
        tickets.encode(tickets.ingest_command(TENANT, "flight_ds", "replace"))
    )
    writer, _ = client.do_put(desc, table.schema)
    writer.write_table(table)
    writer.close()
    print("streamed", table.num_rows, "rows in")

    count = trino_client.scalar(f'SELECT count(*) FROM "iceberg"."{TENANT}"."flight_ds"')
    print("trino sees count =", count)
    assert count == 1000, count

    print("\n=== DoGet: stream query result out ===")
    ticket = fl.Ticket(tickets.encode(tickets.query_command(TENANT, "SELECT sum(v) AS s FROM flight_ds")))
    got = client.do_get(ticket).read_all()
    print("streamed result:", got.to_pylist())
    assert got.to_pylist() == [{"s": sum(i * 2 for i in range(1000))}]

    server.shutdown()
    print("\nFLIGHT SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

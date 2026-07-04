"""Arrow Flight streaming data plane (M3).

A gRPC Flight server that lets clients stream Arrow record batches directly
in and out of Memcove, without round-tripping bulk data through MCP tool calls:

  * **DoPut** — descriptor command ``{"op":"ingest","tenant","name","mode"}``;
    the client streams batches, which are written into the dataset's Iceberg
    table (PyIceberg write path).
  * **DoGet** — ticket ``{"op":"read","tenant","name"}`` or
    ``{"op":"query","tenant","sql"}``; the server runs the (guarded) query via
    Trino and streams the result back as Arrow batches.

The control-plane MCP tools ``open_ingest_stream`` / ``stream_dataset`` mint the
descriptors/tickets and advertise this server's URI; clients do the streaming.

Run standalone:  ``memcove-flight``  (or ``python -m memcove.data_plane.flight_server``)

Security: tickets are unsigned for now (auth deferred — see core/tenancy.py);
the tenant in the ticket is trusted exactly like the MCP request header today.
Memory note: DoPut currently buffers the stream server-side before the Iceberg
commit; incremental per-batch commits are a future optimization.
"""

from __future__ import annotations

import logging

import pyarrow as pa
import pyarrow.flight as fl

from memcove.core import catalog, registry, trino_client
from memcove.core.config import get_settings
from memcove.core.models import SourceKind
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select
from memcove.core.tenancy import normalize_tenant
from memcove.data_plane import tickets

logger = logging.getLogger("memcove.flight")


class MemcoveFlightServer(fl.FlightServerBase):
    def __init__(self, location: str | None = None):
        settings = get_settings()
        self._location = location or f"grpc://0.0.0.0:{settings.flight_port}"
        super().__init__(self._location)

    # -- DoGet: stream a dataset / query result out ---------------------------

    def _result_table(self, cmd: dict) -> pa.Table:
        settings = get_settings()
        tenant = normalize_tenant(cmd.get("tenant"))
        op = cmd.get("op")
        if op == "read":
            name = validate_label(cmd["name"])
            guard = validate_select(f"SELECT * FROM {name}", tenant, settings.trino_catalog)
        elif op == "query":
            guard = validate_select(cmd["sql"], tenant, settings.trino_catalog)
        else:
            raise fl.FlightError(f"unsupported DoGet op {op!r}")
        return trino_client.execute_arrow(guard.sql)

    def do_get(self, context, ticket: fl.Ticket):
        try:
            cmd = tickets.decode(ticket.ticket)
            table = self._result_table(cmd)
        except Exception as exc:  # noqa: BLE001
            raise fl.FlightError(str(exc)) from exc
        return fl.RecordBatchStream(table)

    def get_flight_info(self, context, descriptor: fl.FlightDescriptor):
        cmd = tickets.decode(descriptor.command)
        table = self._result_table(cmd)
        endpoint = fl.FlightEndpoint(
            fl.Ticket(tickets.encode(cmd)),
            [fl.Location.for_grpc_tcp("localhost", get_settings().flight_port)],
        )
        return fl.FlightInfo(table.schema, descriptor, [endpoint], table.num_rows, table.nbytes)

    # -- DoPut: stream batches into a dataset ---------------------------------

    def do_put(self, context, descriptor: fl.FlightDescriptor, reader, writer):
        cmd = tickets.decode(descriptor.command)
        if cmd.get("op") != "ingest":
            raise fl.FlightError(f"unsupported DoPut op {cmd.get('op')!r}")
        tenant = normalize_tenant(cmd.get("tenant"))
        name = validate_label(cmd["name"])
        mode = cmd.get("mode", "create")

        # Buffer the streamed batches, then write once (single Iceberg commit).
        table = reader.read_all()
        rows = catalog.write_arrow(tenant, name, table, mode=mode)
        registry.record_object(
            tenant,
            name,
            table_ident=f"{get_settings().trino_catalog}.{tenant}.{name}",
            source=SourceKind.STREAM.value,
            source_ref="flight",
        )
        logger.info("flight ingest: %s.%s += %d rows (mode=%s)", tenant, name, rows, mode)


def serve() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    location = f"grpc://{settings.flight_host}:{settings.flight_port}"
    server = MemcoveFlightServer(location)
    try:
        registry.init_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning("registry init failed (is postgres up?): %s", exc)
    logger.info("Memcove Flight data plane listening on %s", location)
    server.serve()


def main() -> None:  # console-script entrypoint
    serve()


if __name__ == "__main__":
    main()

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

Security: tickets/descriptors are HMAC-signed and short-lived (see tickets.sign /
tickets.verify + flight_ticket_secret); a client cannot forge one for another
tenant, and serve() warns if the signing secret is left at its insecure default.
Memory note: DoPut commits in a single Iceberg commit while an upload stays under
``doput_single_commit_max_rows`` (small uploads stay atomic); past that it flushes
in chunks (multiple commits) so a large upload can't buffer whole in the pod — at
the cost of a partial table if the stream fails mid-way.
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

    def _validated_query(self, cmd: dict) -> tuple[str, str]:
        """Resolve a read/query DoGet command to ``(tenant, guarded_sql)``."""
        settings = get_settings()
        tenant = normalize_tenant(cmd.get("tenant"))
        op = cmd.get("op")
        if op == "read":
            name = validate_label(cmd["name"])
            guard = validate_select(
                f"SELECT * FROM {name}", tenant, settings.trino_catalog,
                shared_schemas=settings.shared_schemas,
            )
        elif op == "query":
            guard = validate_select(
                cmd["sql"], tenant, settings.trino_catalog,
                shared_schemas=settings.shared_schemas,
            )
        else:
            raise fl.FlightError(f"unsupported DoGet op {op!r}")
        return tenant, guard.sql

    def do_get(self, context, ticket: fl.Ticket):
        try:
            cmd = tickets.verify(ticket.ticket)
            tenant, sql = self._validated_query(cmd)
            # Stream batches straight from the Trino cursor; the whole result is never
            # resident in the pod (unlike the old execute_arrow + RecordBatchStream).
            schema, batches = trino_client.stream_arrow_batches(sql, run_as=tenant)
        except Exception as exc:  # noqa: BLE001
            raise fl.FlightError(str(exc)) from exc
        return fl.GeneratorStream(schema, batches)

    def get_flight_info(self, context, descriptor: fl.FlightDescriptor):
        cmd = tickets.verify(descriptor.command)
        tenant, sql = self._validated_query(cmd)
        # Schema only (LIMIT 0) — do NOT run the full query here just to count rows.
        schema = trino_client.result_schema(sql, run_as=tenant)
        endpoint = fl.FlightEndpoint(
            # Re-issue a SIGNED ticket — do_get verifies signatures and would reject
            # an unsigned one, breaking the get_flight_info -> do_get handshake.
            fl.Ticket(tickets.sign(cmd)),
            [fl.Location.for_grpc_tcp("localhost", get_settings().flight_port)],
        )
        # Row/byte totals are unknown without executing the query; -1 signals unknown.
        return fl.FlightInfo(schema, descriptor, [endpoint], -1, -1)

    # -- DoPut: stream batches into a dataset ---------------------------------

    def do_put(self, context, descriptor: fl.FlightDescriptor, reader, writer):
        cmd = tickets.verify(descriptor.command)
        if cmd.get("op") != "ingest":
            raise fl.FlightError(f"unsupported DoPut op {cmd.get('op')!r}")
        tenant = normalize_tenant(cmd.get("tenant"))
        name = validate_label(cmd["name"])
        mode = cmd.get("mode", "create")

        # Hybrid commit: buffer batches and, while the upload stays under the
        # threshold, write once at the end (single Iceberg commit, all-or-nothing —
        # the common small-upload case). Only once a stream crosses the threshold do
        # we flush in chunks so a huge upload can't buffer the whole thing in the pod;
        # the first flush uses the requested mode, later flushes append so earlier
        # chunks aren't overwritten. Trade-off: an over-threshold upload becomes
        # multi-commit, so a mid-stream failure can leave a partially-written table.
        threshold = get_settings().doput_single_commit_max_rows
        buffer: list[pa.RecordBatch] = []
        buffered = 0
        total = 0
        commits = 0

        def _flush() -> None:
            nonlocal buffer, buffered, commits
            if not buffer:
                return
            table = pa.Table.from_batches(buffer)
            catalog.write_arrow(tenant, name, table, mode=(mode if commits == 0 else "append"))
            commits += 1
            buffer = []
            buffered = 0

        for chunk in reader:
            batch = chunk.data
            if batch is None or batch.num_rows == 0:
                continue
            buffer.append(batch)
            buffered += batch.num_rows
            total += batch.num_rows
            if buffered >= threshold:
                _flush()
        _flush()

        if commits == 0:
            # Empty upload: create a zero-row table with the stream's schema so the
            # dataset still exists (matches the old read_all() behavior).
            catalog.write_arrow(tenant, name, reader.schema.empty_table(), mode=mode)

        ok = registry.record_object_guarded(
            tenant,
            name,
            table_ident=f"{get_settings().trino_catalog}.{tenant}.{name}",
            source=SourceKind.STREAM.value,
            source_ref="flight",
        )
        # Data is committed either way; on a registry failure the guarded write has
        # already logged the drift signal for the reconciler / read-repair to backfill.
        logger.info(
            "flight ingest: %s.%s += %d rows (mode=%s, commits=%d, metadata_pending=%s)",
            tenant, name, total, mode, max(commits, 1), not ok,
        )


def serve() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if settings.flight_ticket_secret in ("", "dev-insecure-change-me"):
        logger.warning(
            "flight_ticket_secret is the INSECURE DEFAULT — anyone who can reach the "
            "Flight port can forge tenant-scoped tickets. Set "
            "MEMCOVE_FLIGHT_TICKET_SECRET before exposing this server."
        )
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

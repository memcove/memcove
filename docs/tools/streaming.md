# Streaming (Arrow Flight)

For data too big for inline payloads or capped previews, Memcove has a streaming data
plane built on [Apache Arrow Flight](https://arrow.apache.org/docs/format/Flight.html) (a
gRPC protocol). The MCP tools mint a **signed, expiring ticket**; an Arrow Flight client
then streams record batches directly against the Flight server — **the bytes never pass
through the MCP channel**.

You need `memcove-flight` running (gRPC on `:8815`) in addition to `memcove-server`.

!!! note "Tickets are signed"
    Tickets and descriptors are HMAC-signed and short-lived (`MEMCOVE_FLIGHT_TICKET_SECRET`
    / `MEMCOVE_FLIGHT_TICKET_TTL_SECONDS`, default 300s). A client cannot forge one for
    another tenant. See [Security](../concepts/security.md#streaming-signed-tickets).

## `stream_dataset`

Get an Arrow Flight ticket to **stream a large result back** as Arrow batches — for when
the data is too big for a `query_memory` preview or you want it as live Arrow rather than a
file. This is the **read** side of the streaming data plane.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` \| `null` | `null` | Stream this whole dataset (provide `name` OR `sql`, not both). |
| `sql` | `str` \| `null` | `null` | Stream the result of this SELECT (provide `name` OR `sql`, not both). |

**Returns** — `{flight_uri, transport: "arrow-flight", ticket_b64, how}`. Decode
`ticket_b64` from base64 to get the raw `DoGet` ticket bytes.

**Client flow**

```text
stream_dataset(name|sql)  ->  {flight_uri, ticket_b64}
  client: ticket = base64_decode(ticket_b64)
  client: flight.DoGet(Ticket(ticket))  ->  read Arrow record batches
```

The server verifies the ticket, re-runs the [SQL guard](../concepts/isolation.md), executes
via Trino, and streams the result as `RecordBatchStream`.

---

## `open_ingest_stream`

Open an Arrow Flight channel to **stream a large dataset in** as Arrow batches — for data
too big to send inline to [`remember_dataset`](storing.md#remember_dataset). This is the
**write** side of the streaming data plane.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | — | Name to store the streamed dataset under. |
| `mode` | `"create"` \| `"replace"` \| `"append"` | `"create"` | create = fail if it exists, replace = overwrite, append = add rows. |

**Returns** — `{flight_uri, transport: "arrow-flight", descriptor_command_b64, how}`.
Decode `descriptor_command_b64` and pass it to `FlightDescriptor.for_command(...)`.

**Client flow**

```text
open_ingest_stream(name, mode)  ->  {flight_uri, descriptor_command_b64}
  client: cmd = base64_decode(descriptor_command_b64)
  client: writer, _ = flight.DoPut(FlightDescriptor.for_command(cmd), schema)
  client: writer.write_table(table); writer.close()
```

The server verifies the descriptor, buffers the streamed batches, and writes them into the
dataset's Iceberg table via PyIceberg (a single commit).

For a working reference, see `scripts/flight_smoke.py` in the repo.

!!! tip "When to use which"
    - Small data → `remember_dataset` inline / `query_memory` preview.
    - A parquet file you already have → `start_large_upload`.
    - Truly large in/out as live Arrow → the streaming tools here.

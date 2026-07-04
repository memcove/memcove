# CLI & entry points

Memcove installs two console scripts (defined in `pyproject.toml`). Both read
configuration from the environment / `.env` (see [Settings](../configuration/settings.md)).

## `memcove-server`

The MCP control plane. Initializes the Postgres registry, then serves MCP over **Streamable
HTTP** at `http://{MEMCOVE_HOST}:{MEMCOVE_PORT}/mcp` (default `0.0.0.0:8090`).

```bash
memcove-server
```

This is the process your MCP clients connect to. It handles all 12 tools and 2 resources.

## `memcove-flight`

The Arrow Flight streaming data plane. Serves gRPC at
`grpc://{MEMCOVE_FLIGHT_HOST}:{MEMCOVE_FLIGHT_PORT}` (default `0.0.0.0:8815`).

```bash
memcove-flight
# equivalently: python -m memcove.data_plane.flight_server
```

Only needed if you use the [streaming tools](../tools/streaming.md)
(`stream_dataset` / `open_ingest_stream`). On startup it warns loudly if
`MEMCOVE_FLIGHT_TICKET_SECRET` is still the insecure default.

## Running both

!!! note "Run them as separate processes"
    In production, run `memcove-server` and `memcove-flight` as two separate
    deployments/processes so they scale independently. Clients dial the Flight server at
    `MEMCOVE_FLIGHT_ADVERTISE_URI` (what the control plane advertises in its tickets),
    which may differ from the bind address.

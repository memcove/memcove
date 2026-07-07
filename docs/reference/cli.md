# CLI & entry points

Memcove installs these console scripts (defined in `pyproject.toml`). All read
configuration from the environment / `.env` (see [Settings](../configuration/settings.md)):

| Command | Role |
| --- | --- |
| `memcove-server` | MCP control plane (HTTP :8090) — the process clients connect to |
| `memcove-flight` | Arrow Flight streaming data plane (gRPC :8815) |
| `memcove-reconcile` | one-shot registry ⇄ catalog reconcile (run on a schedule) |
| `memcove-bench` | throughput benchmark (needs the `bench` extra) |
| `memcove-dcf` | DCF valuation pipeline (needs the `bench` extra) |

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

## `memcove-reconcile`

A one-shot job that diffs the Iceberg catalog against the metadata registry — backfilling
rows for tables written while the registry was down and dropping dangling ones (see the
[reconciler guardrails](../configuration/settings.md#reconciler-self-healing)). Run it on a
schedule; the Helm chart ships it as a CronJob.

```bash
memcove-reconcile
```

## Benchmarks & example workloads

Two model-free workloads that exercise Memcove with real market data (yfinance). They
require the `bench` extra:

```bash
pip install 'memcove[bench]'      # or: uv sync --extra bench

memcove-bench --years 8 --replicate 4     # ingest + multi-hop DAG throughput benchmark
memcove-dcf AAPL                          # DCF valuation for one or more tickers
memcove-dcf --method ebit-fcff MSFT GOOGL
```

Both fall back to deterministic synthetic data (`--synthetic`) so they run offline. See
`benchmarks/README.md` in the repo for the full workload description.

# Quickstart

Get Memcove running locally against a full lakehouse stack (MinIO, an Iceberg REST
catalog, Trino, and Postgres) in Docker, then start the MCP server.

## Prerequisites

- **Docker** (with Compose) for the data-plane stack.
- **Python 3.12+**. [`uv`](https://docs.astral.sh/uv/) is recommended; `pip` works too.

## 1. Bring up the data plane

```bash
docker compose up -d --wait
```

This starts five services (Compose project `memcove`):

| Service | Image | Host port | Purpose |
| --- | --- | --- | --- |
| `minio` | `minio/minio` | 9000 (API), 9001 (console) | S3-compatible object store |
| `minio-init` | `minio/mc` | — | one-shot: creates the `warehouse`, `memcove-staging`, `memcove-artifacts` buckets |
| `iceberg-rest` | `apache/iceberg-rest-fixture` | 8181 | Iceberg REST catalog |
| `trino` | `trinodb/trino` | 8080 | query engine (read / derive / export) |
| `postgres` | `postgres:16` | 5433 | control-plane metadata & lineage registry |

The `--wait` flag blocks until every service (Trino included) reports healthy, so
there's no need to guess at a startup delay. The MinIO console is at
`http://localhost:9001` (user `minio`, password `minio12345`).

An optional `mysql` service (for exercising the MySQL registry backend) sits behind a
Compose profile and is skipped by default — start it with
`docker compose --profile mysql up -d --wait`.

## 2. Install Memcove

```bash
uv sync --extra dev        # or: pip install -e ".[dev]"
cp .env.example .env
```

The defaults in `.env.example` already point at the local Docker stack, so no edits are
needed for local dev. See the [settings reference](../configuration/settings.md) for
what each variable does.

## 3. Run the tests

```bash
uv run pytest -m "not integration"     # fast unit tests, no stack needed
uv run pytest -m integration           # end-to-end, requires the stack from step 1
```

You can also drive the full agent journey in-process:

```bash
uv run python scripts/smoke.py         # ingest -> query -> derive -> export -> isolation check
uv run python scripts/flight_smoke.py  # Arrow Flight stream in/out
```

## 4. Start the servers

```bash
memcove-server     # MCP control plane, Streamable HTTP on :8090
memcove-flight     # Arrow Flight data plane, gRPC on :8815 (only needed for streaming)
```

`memcove-server` initializes the Postgres registry on startup and serves MCP over
Streamable HTTP at `http://localhost:8090/mcp`.

!!! tip "No OIDC proxy needed for local development"
    Memcove trusts a tenant header set by an upstream proxy in production, but you do
    **not** need a proxy to develop. See
    [Local development (no proxy)](../configuration/local-dev.md).

## Next

- [Connect an MCP client](connecting.md) and make your first call.
- Follow the [walkthrough](walkthrough.md) to store, query, derive, and export data.

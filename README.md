# Memcove

A **lakehouse-backed memory service for LLM agents**, exposed over **MCP**.

Agents dump data into labeled **memory objects** (inline dataframe, an `s3://`
parquet reference, or an out-of-band upload), ask the service to derive new
objects with SQL (joins / aggregations / filters), and either read results back
or get an **exported artifact** as a presigned URL. Objects are Iceberg tables
in a Trino-backed catalog.

## Architecture — two planes

- **Control plane = this MCP server.** Metadata, SQL/derivation, capped previews,
  artifact URIs, presigned-upload handles. This is all the LLM touches.
- **Data plane = S3 + Trino/Iceberg.** Bulk bytes never travel through MCP tool
  responses; the model gets handles, previews, and presigned URLs instead.

Write vs read split:
- **PyIceberg + PyArrow** = the ingest/write path (`core/catalog.py`).
- **Trino** = the read/derive/export path (`core/trino_client.py`).

Isolation is **private per tenant** (`<tenant>.<label>` → Iceberg table
`iceberg.<tenant_ns>.<label>`), enforced by the **SQL guard**
(`core/sql_guard.py`): only read-only SELECTs, every table reference qualified to
the caller's namespace, cross-namespace/catalog references rejected.

> Auth is deferred. The tenant is read from the `x-memcove-tenant` header today;
> when real auth lands, only `core/tenancy.py` changes.

## MCP tools

Named as a **memory family** so agents reach for them by intent:

| tool | purpose |
|------|---------|
| `remember_dataset(name, source, mode, tags)` | store data: inline / `s3_parquet` / `upload_handle` |
| `query_memory(sql, limit)` | guarded read-only SELECT over datasets, capped preview |
| `derive_dataset(new_name, sql, mode, tags)` | persist a computed table (CTAS) + lineage |
| `recall_dataset(name, mode)` | read one dataset: `preview` \| `schema` \| `stats` |
| `inspect_dataset(name)` | schema, source, tags, lineage, row count |
| `list_memory(tags)` | list a tenant's datasets |
| `export_dataset(fmt, name\|sql)` | materialize to S3, return presigned URL |
| `start_large_upload(name)` | presigned PUT URL for out-of-band parquet upload |
| `forget_dataset(name)` | permanently delete a dataset |

Each description spells out *when to use this vs. its neighbors*, and the server
ships an `instructions` block framing the whole toolkit. Resources:
`memcove://{tenant}/{name}` and `memcove://{tenant}/_catalog`.

## Quickstart

```bash
# 1. bring up the local lakehouse (Trino + MinIO + Iceberg REST + Postgres)
docker compose up -d --wait  # blocks until Trino & friends report healthy

# 2. install + configure
uv sync --extra dev         # or: pip install -e ".[dev]"
cp .env.example .env

# 3. unit tests (no infra needed)
pytest -m "not integration"

# 4. end-to-end smoke against the running stack
python scripts/smoke.py
pytest -m integration

# 5. run the MCP server (Streamable HTTP on :8090)
memcove-server
```

Point an MCP client (e.g. MCP Inspector) at `http://localhost:8090/mcp` and send
`x-memcove-tenant: <your-tenant>` to scope your namespace.

## Agentic demos (local LLM via LM Studio)

Two scripts drive Memcove with a local OpenAI-compatible model (LM Studio on
`:1234`). Both bridge the **real MCP tools** into OpenAI function-calling, so the
model sees the actual tool descriptions.

```bash
memcove-server                                   # MCP server must be running
uv run python scripts/agent_demo.py --dry-run  # just print the bridged tool specs
uv run python scripts/agent_demo.py            # fully autonomous agent loop
uv run python scripts/pipeline_demo.py         # guided pipeline (always completes)
```

- **`agent_demo.py`** — the model autonomously plans and calls tools to build the
  warehouse. Best with a strong, tool-capable model; small models may stall.
- **`pipeline_demo.py`** — the script orchestrates the lifecycle and uses the LLM
  for what it's good at (inventing data, authoring SQL, narrating findings), with
  a fallback at every step so it reliably produces the final result: invent
  `customers`/`products`/`orders` → derive `order_facts`/`revenue_by_*`/
  `monthly_revenue`/`top_customers` (joins + rollups, lineage tracked) → export
  the leaderboard CSV → narrative. Recommended for a dependable end-to-end run.

## Roadmap

- **M3 (fast-follow):** Arrow Flight streaming data plane
  (`src/memcove/data_plane/flight_server.py`).
- **Later:** real auth (bearer → OAuth 2.1) behind the `core/tenancy.py` seam.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev
setup, test/lint gates, and PR flow. By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md). To report a security issue, see
[SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).

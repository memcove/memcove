# Memcove

**Durable, queryable memory for LLM agents — so they can work with real datasets
instead of stuffing everything into their context window.**

## The problem

Agents are increasingly asked to work with **real data**: a few million rows of orders, a
pile of parquet files, the result of a query. But an agent's only working memory is its
**context window**, and that's a bad place to put data:

- **It doesn't scale.** Even a million-token context holds only a few megabytes of text —
  real datasets dwarf that, and filling the window is slow, costly, and lossy.
- **It doesn't persist.** Whatever's in the window is gone when the session ends or the
  context is compacted.
- **It can't compute.** Ask a model to sum a column or join two tables in its head and it
  will quietly miscount, drop rows, and invent totals. LLMs reason; they don't aggregate.

So the moment an agent needs to *remember* a dataset or *actually compute* over one —
*"join last quarter's orders with this month's returns and give me revenue by region"* —
the context window is the wrong tool.

## How people handle this today

A few common workarounds, each with a real limit:

- **Paste it into the prompt.** Fine for a handful of rows; falls apart at any real size —
  token cost, truncation, and arithmetic the model can't be trusted to do.
- **Vector search / RAG** (embeddings — mem0, Letta, a vector DB). The default "agent
  memory," and great for *semantic recall over text* — but it can't answer a `GROUP BY`.
  Retrieval returns approximate chunks, not exact aggregates; it's memory for documents,
  not for tables.
- **A code sandbox** (a Python/pandas REPL, e.g. a code interpreter). This computes
  *correctly* — but the data has to fit in one process's RAM, the sandbox is ephemeral (no
  memory across sessions), and there's no isolation across many agents or users.
- **Raw SQL access to a warehouse.** Scales and computes — but handing an LLM a live
  connection to Snowflake / BigQuery / Postgres is a liability: a prompt-injected `DELETE`,
  a cross-tenant read, or an unbounded scan that runs up a bill. And every team rebuilds the
  same plumbing.

None of these is *durable, large-scale, correct, and agent-safe at the same time.*

## Where Memcove fits

Memcove is that missing layer — a memory service an agent talks to over
[MCP](https://modelcontextprotocol.io), built so an LLM can safely store and compute over
real datasets. The agent works with data **by name**; the data itself stays out of its
context:

1. **Remember** a dataset under a name — inline rows, an `s3://` parquet file, or a direct
   upload.
2. **Derive** new datasets with plain SQL — **joins and aggregations over datasets far too
   big to fit in a prompt** (millions to billions of rows, across many tables) — which
   Memcove runs in the lakehouse and records the lineage of.
3. **Read back** only what's needed: a capped preview, or a download link for the full
   result.

It combines what those workarounds each only half-do:

- **Durable**, like a warehouse — datasets persist across turns, sessions, and other agents.
- **Large-scale and correct**, like the sandbox but unbounded — joins and aggregations run
  in a distributed SQL engine over a lakehouse: exact, and not limited by RAM or context.
- **Agent-safe by construction**, unlike raw warehouse access — a read-only SQL guard,
  per-tenant isolation, capped previews, and no bulk bytes through the model.
- **Structured**, where RAG is semantic — memory for *tables*, meant to sit *alongside* RAG,
  not replace it.

The data lives in a real lakehouse and never passes through the model's context; the agent
only ever sees names, previews, and links.

## How it works

Two planes keep the bytes away from the model:

- **Control plane = this MCP server.** Metadata, SQL/derivation, capped previews, artifact
  URIs, presigned-upload handles. This is all the LLM touches.
- **Data plane = S3 + Trino/Iceberg.** Bulk bytes never travel through MCP tool responses;
  the model gets handles, previews, and presigned URLs instead.

Datasets are Iceberg tables in a Trino-backed catalog. The write and read paths are split:

- **PyIceberg + PyArrow** = the ingest/write path (`core/catalog.py`).
- **Trino** = the read/derive/export path (`core/trino_client.py`).

Isolation is **private per tenant** (`<tenant>.<label>` → Iceberg table
`iceberg.<tenant_ns>.<label>`), enforced by the **SQL guard** (`core/sql_guard.py`): only
read-only SELECTs, every table reference qualified to the caller's namespace,
cross-namespace/catalog references rejected.

**Auth**: two models, both resolving to the tenant namespace through the single
`core/tenancy.py` seam — a **trusted-header / proxy** mode (default) and **native OAuth
2.1**, where Memcove validates bearer JWTs itself so clients like Claude connect directly.
See the [auth docs](https://memcove.github.io/memcove/configuration/auth/).

## MCP tools

Named as a **memory family** so agents reach for them by intent:

| tool | purpose |
|------|---------|
| `remember_dataset(name, source, mode, tags, target)` | store data: inline / `s3_parquet` / `upload_handle` (`target=lakehouse\|scratch`) |
| `query_memory(sql, limit)` | guarded read-only SELECT over datasets, capped preview |
| `derive_dataset(new_name, sql, mode, tags, target)` | persist a computed table (CTAS) + lineage (`target=lakehouse\|scratch`) |
| `recall_dataset(name, mode)` | read one dataset: `preview` \| `schema` \| `stats` |
| `inspect_dataset(name)` | schema, source, tags, lineage, row count |
| `list_memory(tags)` | list a tenant's datasets |
| `export_dataset(fmt, name\|sql)` | materialize to S3, return presigned URL |
| `discover_reference_data()` | list the shared read-only reference schemas |
| `start_large_upload(name)` | presigned PUT URL for out-of-band parquet upload |
| `forget_dataset(name)` | permanently delete a dataset |
| `stream_dataset(name\|sql)` | Arrow Flight: stream a dataset/query out (bulk read) |
| `open_ingest_stream(name, mode)` | Arrow Flight: stream bulk rows in |

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
uv run pytest -m "not integration"

# 4. end-to-end smoke against the running stack
uv run python scripts/smoke.py
uv run pytest -m integration

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

## Beyond the core

Shipped on top of the control/data-plane core (see the
[CHANGELOG](CHANGELOG.md) and [docs](https://memcove.github.io/memcove/) for detail):

- **Arrow Flight streaming data plane** (`stream_dataset` / `open_ingest_stream`) for
  bulk in/out that bypasses MCP responses.
- **Native OAuth 2.1** resource server alongside the trusted-header/proxy model.
- **Pluggable registry** — SQLite (zero-setup local), Postgres, or MySQL.
- **Scratchpad plane** — an optional ephemeral DuckDB-behind-Trino store you can `JOIN`
  with lakehouse and reference tables in one query.
- **Container image + Helm chart** for Docker/Kubernetes deployment.
- **Example workloads** (`memcove-bench`, `memcove-dcf`) that drive Memcove with real
  market data — see [`benchmarks/`](benchmarks/README.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev
setup, test/lint gates, and PR flow. By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md). To report a security issue, see
[SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).

# Memcove

**Memcove is persistent, queryable data memory for LLM agents.** Instead of holding
tabular data in the conversation, an agent stores it as named datasets that survive
across turns and other agents, then computes over them with SQL — joins, rollups,
filters — and hands back previews, files, or live Arrow streams.

It speaks the [Model Context Protocol (MCP)](https://modelcontextprotocol.io), so any
MCP-capable agent can use it as a tool. Underneath, datasets are Apache Iceberg tables
in an object store, queried through Trino.

```text
  1. remember_dataset   store a dataframe / file / query result
  2. query_memory       explore it with read-only SQL
  3. derive_dataset     save computed tables (joins/rollups) with lineage
  4. export_dataset     hand the user a downloadable file
```

## Two planes

Memcove separates the small control traffic an agent touches from the bulk data it
never should.

- **Control plane** — the MCP server. Metadata, SQL/derivation requests, capped row
  previews, artifact URLs, and presigned upload handles. This is everything the LLM sees.
- **Data plane** — S3 (object store) + Trino/Iceberg (query engine + catalog) + a
  Postgres registry + an Arrow Flight streaming server. Bulk bytes move here and never
  round-trip through the MCP channel.

A hard invariant runs through the design: **writes go through PyIceberg; reads,
derivations, and exports go through Trino** — and every operation is confined to the
caller's private tenant namespace.

## Key concepts

- **Dataset** — a named table (`signups`, `revenue_by_user`). Reference it by its bare
  name in SQL. Datasets are private to your tenant.
- **Tenant** — the isolation boundary. Each caller maps to a private Iceberg schema
  (`t_<id>`); you can only see and query your own datasets.
- **Shared reference plane** — optional read-only schemas (e.g. `ref_market`) every
  tenant can query but none can write. Discover them with `discover_reference_data`.
- **Lineage** — `derive_dataset` records which datasets and SQL produced a result, so
  you can audit provenance with `inspect_dataset`.

## Where to go next

<div class="grid cards" markdown>

- :material-rocket-launch: **[Quickstart](getting-started/quickstart.md)** — run the
  stack locally with Docker in a few minutes.
- :material-connection: **[Connect an MCP client](getting-started/connecting.md)** —
  point your agent at Memcove.
- :material-tools: **[MCP tool reference](tools/index.md)** — all 12 tools, verbatim.
- :material-sitemap: **[Architecture](concepts/architecture.md)** — how the pieces fit.
- :material-shield-lock: **[Security & isolation](concepts/security.md)** — the trust
  boundary and how tenants stay separated.
- :material-cog: **[Configuration](configuration/settings.md)** — every setting.

</div>

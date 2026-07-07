# BYO Trino & Iceberg catalog

Memcove does **not** deploy or manage Trino, an Iceberg catalog, or an object store — you
bring your own and point Memcove at them. This page states exactly what Memcove assumes
of those systems so a self-hosted setup works the first time, and what's configurable.

## The write/read split

One invariant shapes every requirement here:

> **Writes go through PyIceberg; reads, derivations, and exports go through Trino.**

`remember_dataset` and the Flight data plane write Iceberg tables via **PyIceberg**
against your **Iceberg REST catalog**. `query_memory`, `derive_dataset`, and
`export_dataset` run SQL through **Trino**. For this to work, *both engines must see the
same tables* — Trino's Iceberg catalog and Memcove's REST catalog have to be the **same
physical catalog over the same object store**.

```text
                    ┌─────────────── same catalog + same storage ───────────────┐
 remember_dataset ──┤  PyIceberg ─► Iceberg REST catalog ─► S3                    │
 query / derive   ──┤  Trino (iceberg connector) ─► same REST catalog ─► same S3  │
                    └────────────────────────────────────────────────────────────┘
```

If Trino points at a different catalog or bucket than `MEMCOVE_ICEBERG_*`, a dataset
written by an agent won't be queryable — that's the most common misconfiguration.

## What Memcove assumes of Trino

- **Trino ≥ 431.** `derive_dataset` with `mode=replace` uses `CREATE OR REPLACE TABLE` on
  the Iceberg connector for an atomic swap; that syntax needs Trino 431 or newer. The dev
  stack pins 480 (which also satisfies the scratchpad's ≥ 480 requirement). Older Trino
  will fail derivations.
- **An Iceberg catalog** configured with the Iceberg connector, pointed at the *same* REST
  catalog and warehouse Memcove writes to. Its Trino catalog name must match
  `MEMCOVE_TRINO_CATALOG` (default `iceberg`).
- **Schema/table DDL rights** for the connect principal (`MEMCOVE_TRINO_USER`): Memcove
  creates a per-tenant schema on first write and issues `CREATE OR REPLACE`/`CREATE TABLE
  AS` for derivations.
- **Catalog metadata is readable** by that principal — Memcove introspects table schemas
  through Trino. (Agents themselves are denied `information_schema`; that restriction is
  Memcove's SQL guard, not something you configure on Trino.)
- **A reachable HTTP(S) endpoint.** Set `MEMCOVE_TRINO_HOST` / `MEMCOVE_TRINO_PORT`, and
  `MEMCOVE_TRINO_HTTP_SCHEME=https` for a TLS-fronted Trino.

### Example Trino catalog config

A matching `iceberg.properties` on your Trino cluster (REST catalog + S3), lining up with
Memcove's `MEMCOVE_ICEBERG_*` and `MEMCOVE_S3_*`:

```properties
connector.name=iceberg
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://iceberg-rest.internal:8181
iceberg.rest-catalog.warehouse=s3://my-memcove-warehouse/
fs.native-s3.enabled=true
s3.region=us-east-1
# For non-AWS/MinIO, also set s3.endpoint and s3.path-style-access=true
```

## What Memcove assumes of the catalog & store

- **An Iceberg REST catalog** at `MEMCOVE_ICEBERG_REST_URI`, with warehouse
  `MEMCOVE_ICEBERG_WAREHOUSE` and catalog name `MEMCOVE_ICEBERG_CATALOG_NAME`. (Only the
  REST catalog type is wired today.)
- **An S3-compatible object store** (real S3, MinIO, etc.) reachable by *both* Memcove and
  Trino. Memcove uses static keys (`MEMCOVE_S3_ACCESS_KEY`/`_SECRET_KEY`) or, when those
  are empty, the AWS default credential chain (IRSA / instance profile / STS). Set
  `MEMCOVE_S3_ENDPOINT` + `MEMCOVE_S3_PATH_STYLE=true` for MinIO or S3-compatible stores.
- **Three buckets/prefixes** exist: warehouse, staging, artifacts
  (`MEMCOVE_WAREHOUSE_BUCKET` / `MEMCOVE_STAGING_BUCKET` / `MEMCOVE_ARTIFACTS_BUCKET`).

## What's configurable vs fixed

| Configurable | Fixed (out of scope) |
| --- | --- |
| Trino host/port/scheme, connect user, catalog name | Trino as the read/derive engine — not pluggable |
| Per-tenant impersonation + your grant backend | The SQL guard is **Trino-dialect**; agents write Trino SQL |
| Session resource caps (`MEMCOVE_TRINO_SESSION_PROPERTIES`) | The catalog name is baked into stored table identifiers |
| Object store endpoint, region, path-style, credentials | Iceberg **REST** catalog type (other catalog types not wired) |
| Warehouse / staging / artifacts buckets | Write = PyIceberg, read = Trino |

## Optional: the scratchpad (DuckDB behind Trino)

If you enable the [scratchpad plane](../concepts/scratchpad.md)
(`MEMCOVE_SCRATCH_ENABLED=true`), Trino needs a bit more:

- **Trino ≥ 480** for the bundled DuckDB connector.
- A **DuckDB catalog** Trino can reach. In `shared` mode you provide a static catalog
  (e.g. `scratch.properties` with `connector.name=duckdb`); in `per_tenant` mode you must
  enable **dynamic catalog management** (`catalog.management=dynamic`) and grant the
  service principal CREATE/DROP CATALOG.
- The DuckDB file(s) on storage **every Trino node can reach** (a shared volume), since
  DuckDB is an embedded, single-writer, single-file engine.

Scratch is entirely optional and off by default — the lakehouse/reference story above
doesn't require any of it. See the [scratchpad concepts page](../concepts/scratchpad.md)
for the mode trade-offs.

## Defense in depth: impersonation

By default Memcove connects to Trino as one service principal and relies on its
[SQL guard](../concepts/isolation.md) for isolation. For a second, independent layer set
`MEMCOVE_TRINO_IMPERSONATION=true` so each query runs to Trino **as the tenant**, and
configure a Trino grant backend (file rules, Ranger, OPA, or Iceberg REST authz). The
service principal then needs impersonation rights, and each tenant principal needs read on
its own schema + the shared schemas and write on its own. Details in
[Authentication & tenancy](../configuration/auth.md#trino-impersonation-defense-in-depth).

!!! warning "Restrict Trino to Memcove"
    Whether or not you use impersonation, only Memcove should be able to reach Trino. If
    tenants can hit Trino directly, both the SQL guard and impersonation are sidesteppable.
    See the [production checklist](checklist.md).

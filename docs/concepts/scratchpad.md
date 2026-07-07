# Scratchpad plane

The **scratchpad** is a fast, small, ephemeral store for data an agent doesn't need to
keep — intermediate results, a staging table for a multi-step computation, a throwaway
join key set. It lives **behind Trino** as a [DuckDB](https://duckdb.org) catalog, so a
scratch dataset can be **joined with durable lakehouse tables and the reference plane in
one query**. It's disabled by default (`MEMCOVE_SCRATCH_ENABLED=true` to turn it on) and
requires **Trino ≥ 480** (the bundled DuckDB connector).

## Three domains, one query surface

Everything an agent queries goes through the single Trino surface, so one guarded SELECT
can span all three:

| Domain | Store | Lifetime | Addressed as |
| --- | --- | --- | --- |
| **Lakehouse** | Iceberg on S3 | durable | bare name — `signups` |
| **Reference** | shared read-only schemas | durable, operator-managed | `ref_market.prices` |
| **Scratch** | DuckDB behind Trino | ephemeral | `scratch.tmp` |

## Using it

Both write tools take `target="scratch"`; queries are unchanged — reference a scratch
dataset with the reserved `scratch.<name>` alias.

```python
# Compute a small scratch table from a big lakehouse table (pure SQL, no data leaves Trino)
derive_dataset(new_name="adults", target="scratch",
               sql="SELECT id, name FROM people WHERE age >= 18")

# Stash small inline data directly into scratch
remember_dataset(name="weights", target="scratch",
                 source={"kind": "inline", "format": "json_records",
                         "records": [{"k": "a", "w": 0.4}, {"k": "b", "w": 0.6}]})

# Join scratch with the lakehouse in ONE query
query_memory(sql="SELECT p.name FROM people p JOIN scratch.adults a ON a.id = p.id")
```

Scratch supports `derive` (from any query) and `remember` with **inline** sources only
(it's for small data). `s3_parquet` / `upload_handle` / Flight always target the
lakehouse. Scratch datasets are **not** recorded in the durable registry — they have no
lineage and aren't returned by `list_memory`; they're meant to be transient.

!!! note "Isolation"
    A caller's `scratch.<name>` always resolves to *their own* scratch schema. One tenant
    can never read another's scratch data — the SQL guard qualifies the alias to the
    caller's namespace, exactly as it does for lakehouse tables.

## Two catalog modes

How the DuckDB catalog is provisioned is an operator choice
(`MEMCOVE_SCRATCH_CATALOG_MODE`), trading simplicity against isolation:

=== "shared (default)"

    One static `scratch` DuckDB catalog you configure in Trino (a single DuckDB file);
    tenants are isolated by a schema per tenant plus the SQL guard.

    - **Pro:** simplest — one catalog properties file, works on stock Trino 480.
    - **Con:** DuckDB is single-writer, so the one file serializes writes across all
      tenants (fine for light scratch use; a bottleneck under heavy concurrent writes).

    ```properties title="etc/catalog/scratch.properties"
    connector.name=duckdb
    connection-url=jdbc:duckdb:/data/scratch/scratch.duckdb
    ```

=== "per_tenant"

    Memcove creates a `scratch_<tenant>` catalog per tenant at runtime, each backed by its
    own DuckDB file.

    - **Pro:** file-level isolation and **no cross-tenant write contention** — each tenant
      writes only its own file; the catalog name encodes the tenant.
    - **Con:** requires Trino **dynamic catalog management**
      (`catalog.management=dynamic`) and the service principal to hold CREATE/DROP CATALOG;
      the per-tenant files must live on storage every Trino node can reach; catalogs
      proliferate with tenant count. Set `MEMCOVE_SCRATCH_DUCKDB_DIR` to the shared
      directory for the `<tenant>.duckdb` files.

## Constraints to know

- **Single-writer per file.** DuckDB allows one writer per file. In `shared` mode that's
  one writer for *all* tenants; in `per_tenant` mode it's one per tenant (concurrent
  sessions of the *same* tenant still serialize).
- **Multi-node Trino.** Every Trino node opens the DuckDB file(s), so they must sit on
  storage all nodes can reach (a shared volume), or run Trino single-node.
- **Scratch requires Trino.** Unlike the lakehouse write path, scratch is defined entirely
  through Trino — there's no Trino-less scratch mode. This is the deliberate trade for
  making scratch joinable with everything else in one query.
- **Ephemeral by design.** Scratch has no durability or lineage guarantees. A
  session/TTL sweep that reclaims scratch datasets lands with the session work (M6); until
  then, reuse `mode="replace"` or drop and recreate.

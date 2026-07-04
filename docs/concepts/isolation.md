# Tenant isolation & the SQL guard

Memcove is multi-tenant: every caller sees only its own datasets, plus an optional
shared read-only reference plane. Two mechanisms enforce this — a **namespace scheme**
and the **SQL guard** that all read/derive/export/stream SQL passes through.

## Private per-tenant namespaces

Each tenant maps to a private Iceberg schema named `t_<id>`. `tenancy.normalize_tenant`
validates and normalizes the raw id (regex `^[a-z][a-z0-9_]{1,62}$`, invalid characters
replaced with `_`). A dataset labeled `signups` for tenant `acme` is physically the
Iceberg table `iceberg.t_acme.signups`.

Isolation is layered:

- Every **registry** row (metadata, lineage) is keyed by tenant.
- Ingest **binds upload handles** to the caller: a handle must be prefixed
  `uploads/{tenant}/`, so one tenant cannot ingest another's pending upload.
- All **SQL** is rewritten and validated by the guard (below).
- Optionally, **Trino impersonation** re-checks access per tenant beneath the guard as
  defense-in-depth (see [Security](security.md)).

## The shared reference plane

Beyond private data, operators can expose read-only reference schemas (configured via
`MEMCOVE_SHARED_SCHEMAS`, default `["ref_market"]`) that **every tenant can query but
none can write**. These resolve to themselves rather than being rewritten into a tenant
namespace. Agents discover them with
[`discover_reference_data`](../tools/reading.md#discover_reference_data) and read them by
qualified name, e.g. `SELECT * FROM ref_market.prices`.

Nothing in the write path (PyIceberg) or the derive CTAS path ever targets these schemas,
so they are effectively read-only to tenants. Use **per-domain** schemas
(`ref_market`, `ref_reference`, …) rather than one catch-all to contain blast radius.

## What the SQL guard does

`sql_guard.validate_select(sql, tenant_ns, catalog, shared_schemas)` is the single choke
point for every read, derive, export, and stream query. It parses with sqlglot (Trino
dialect) and enforces:

**Read-only, single statement.** Exactly one statement, and the top node must be a
`SELECT` / `UNION` / `INTERSECT` / `EXCEPT` / subquery. `INSERT`, `UPDATE`, `DELETE`,
`MERGE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, and raw commands are rejected. Agents
never write SQL DDL/DML — persistence is API-driven through `derive_dataset`, which builds
the CTAS itself from a *validated* SELECT.

**Table-reference resolution.** For every physical table reference, comparing names
case-folded (so a quoted mixed-case identifier can't smuggle a foreign schema):

| Reference | Result |
| --- | --- |
| bare name (`signups`) or your own schema (`t_acme.signups`) | rewritten to `t_<tenant>.signups` |
| a schema in `shared_schemas` (`ref_market.prices`) | resolves to itself |
| a CTE name | left alone (not a physical table) |
| any other tenant's schema | **rejected** (cross-namespace) |
| a foreign catalog | **rejected** |
| a metadata/enumeration schema (`information_schema`, `system`, `jdbc`, `metadata`, `sys`, `pg_catalog`) | **rejected** |
| an unclassifiable FROM item (e.g. `TABLE(system.query(...))`) | **rejected** (fail closed) |

So `SELECT * FROM signups` becomes `SELECT * FROM "iceberg"."t_acme"."signups"` before it
ever reaches Trino, and `SELECT * FROM t_other.secrets` never runs.

The guard returns a `GuardedQuery` with the rewritten SQL and the list of referenced
labels (used to compute lineage on derive). `wrap_preview` additionally caps a validated
SELECT at `row_cap + 1` rows so callers can detect truncation, while preserving
`ORDER BY`.

!!! note "Why fail-closed matters"
    With `MEMCOVE_TRINO_IMPERSONATION` off (the default), the guard is the *only*
    isolation layer, so it rejects anything it cannot positively classify rather than
    passing it through. Turning impersonation on adds Trino's own access control beneath
    it as a second, independent layer.

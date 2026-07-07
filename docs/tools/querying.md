# Querying & deriving

Ask questions of stored data with read-only SQL, and save results worth keeping. The SQL
runs in **Trino over the lakehouse**, not in the agent — so joins and aggregations span
datasets **far larger than any context window** (millions to billions of rows, across many
tables) and only the small, capped result comes back to the model.

## `query_memory`

Run a read-only SQL `SELECT` over your datasets and get a capped preview back. This is the
main way to **ask questions** of stored data — filters, joins, aggregations, anything
SELECT.

Reference datasets by their bare name, e.g.
`SELECT region, count(*) FROM signups GROUP BY region`. Only read queries are allowed
(`SELECT` / `WITH` / `UNION`); it cannot modify data.

- To **save** a result as a new reusable dataset, use [`derive_dataset`](#derive_dataset).
- To hand the full result to a user as a file, use [`export_dataset`](exporting.md#export_dataset).
- To dump a whole dataset without writing SQL, use [`recall_dataset`](reading.md#recall_dataset).

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `sql` | `str` | — | A read-only SQL SELECT. Reference datasets by their bare name. |
| `limit` | `int` \| `null` | `null` | Max rows to return in the preview. |

**Returns** — `{columns, rows, row_count, truncated}`. `truncated` is `true` if more rows
exist beyond the cap (`MEMCOVE_PREVIEW_ROW_CAP`, default 1000) — narrow the query or use
`export_dataset` for everything.

!!! info "Under the hood"
    The SQL is validated and rewritten by the [SQL guard](../concepts/isolation.md) (every
    table reference qualified to your tenant, cross-tenant/metadata references rejected),
    capped, and run through Trino.

---

## `derive_dataset`

Create a **new** named dataset from a SQL SELECT over existing datasets and persist it — a
join, rollup, or filtered view you want to keep and reuse. Lineage back to the source
datasets is recorded automatically (visible via [`inspect_dataset`](reading.md#inspect_dataset)).

Use this instead of `query_memory` when the result is worth keeping. Use
[`remember_dataset`](storing.md#remember_dataset) instead when the data comes from
**outside** (inline/file/upload) rather than from a query.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `new_name` | `str` | — | Name for the new dataset to create. |
| `sql` | `str` | — | A read-only SELECT over existing datasets, referenced by bare name. |
| `mode` | `"create"` \| `"replace"` | `"create"` | create = fail if it exists, replace = overwrite. |
| `tags` | `list[str]` \| `null` | `null` | Optional labels to organize and later filter datasets. |
| `target` | `"lakehouse"` \| `"scratch"` | `"lakehouse"` | Where to materialize the result — `scratch` uses the ephemeral [scratchpad plane](../concepts/scratchpad.md). |

**Returns** — the new dataset's name, schema, row count, and lineage.

**Example**

```json
{
  "new_name": "revenue_by_user",
  "sql": "SELECT u.id, sum(o.amount) AS revenue FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.id"
}
```

!!! info "Under the hood"
    The validated SELECT is wrapped in a Trino `CREATE TABLE <tenant>.<new_name> AS ...`.
    You never write DDL — persistence is API-driven, and the CTAS is built from the
    guard-rewritten SELECT. Parents that exist as your tables are recorded as lineage.

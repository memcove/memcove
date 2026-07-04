# Reading & discovering

Read datasets directly, audit their provenance, list what you have, and discover shared
reference data — all without writing SQL.

## `recall_dataset`

Read a named dataset directly, without writing SQL.

Use this for a quick look at one dataset. To filter/join/aggregate, use
[`query_memory`](querying.md#query_memory); for provenance/lineage use
[`inspect_dataset`](#inspect_dataset).

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | — | Name of the dataset to read. |
| `mode` | `"preview"` \| `"schema"` \| `"stats"` | `"preview"` | preview = first rows, schema = columns/types, stats = row count + schema. |
| `limit` | `int` \| `null` | `null` | Max rows when `mode=preview`. |

**Returns** — depends on `mode`: capped rows (`preview`), column names/types (`schema`),
or row count + schema (`stats`).

---

## `inspect_dataset`

Get full metadata for a dataset: schema, where it came from (source), tags, row count, and
its **lineage** — which datasets and SQL produced it.

Use this to understand or audit a dataset before trusting or building on it. For the actual
rows, use [`recall_dataset`](#recall_dataset) or [`query_memory`](querying.md#query_memory).

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | — | Name of the dataset to inspect. |

**Returns** — a `MemoryObject`: tenant, label, table identifier, source, schema, row/size
counts, tags, timestamps, and `lineage` (`parents` + `producing_sql`).

---

## `list_memory`

List the datasets currently in your memory (name, source, tags).

Start here to discover what's already stored before querying or re-ingesting — it avoids
duplicating data you already have. Optionally filter by `tags`, then use
[`inspect_dataset`](#inspect_dataset) / [`recall_dataset`](#recall_dataset) to dig into one.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `tags` | `list[str]` \| `null` | `null` | Only return datasets carrying any of these tags. |

**Returns** — `{datasets: [{label, source, tags, updated_at}, ...]}`.

---

## `discover_reference_data`

List the shared reference datasets available to every tenant (read-only).

Beyond your own private datasets, Memcove may expose shared reference data (e.g.
market/reference tables) that anyone can query but no one can modify. Use this to see which
shared schemas and tables exist and their columns, then read them in SQL by their qualified
name, e.g. `SELECT * FROM ref_market.prices`.

**Parameters** — none.

**Returns** — `{schemas: [{schema, tables: [{name, columns: [{name, type}]}]}]}`.

The shared schemas are configured by the operator (`MEMCOVE_SHARED_SCHEMAS`, default
`["ref_market"]`). See [Tenant isolation](../concepts/isolation.md#the-shared-reference-plane).

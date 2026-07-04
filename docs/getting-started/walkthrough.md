# Walkthrough

A tour of the core lifecycle: store data, query it, derive a new dataset with lineage,
inspect it, and export a file. This mirrors what `scripts/smoke.py` does end to end.

Each step is a tool call. The examples show the arguments you'd pass; the exact call
syntax depends on your MCP client (see [Connect an MCP client](connecting.md)).

## 1. Store a dataset

`remember_dataset` is how data **enters** Memcove. For small data, send rows inline:

```json
{
  "name": "orders",
  "source": {
    "kind": "inline",
    "format": "json_records",
    "records": [
      {"user_id": 1, "amount": 42.0},
      {"user_id": 1, "amount": 8.0},
      {"user_id": 2, "amount": 100.0}
    ]
  }
}
```

Returns the stored dataset's name, schema, and row count. Other `source` shapes (an
existing `s3://` parquet file, or a large out-of-band upload) are covered in
[Storing data](../tools/storing.md).

Store a second dataset to join against:

```json
{
  "name": "users",
  "source": {"kind": "inline", "format": "json_records",
             "records": [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Lin"}]}
}
```

## 2. Query it

`query_memory` runs a read-only SQL `SELECT`. Reference datasets by their bare name:

```sql
SELECT user_id, sum(amount) AS spent
FROM orders
GROUP BY user_id
ORDER BY spent DESC
```

You get back `{columns, rows, row_count, truncated}` — a preview capped at
`MEMCOVE_PREVIEW_ROW_CAP` (default 1000). `truncated` tells you if more rows exist.

## 3. Derive a new dataset

When a result is worth keeping, `derive_dataset` materializes it as a new named dataset
and records lineage back to its sources:

```json
{
  "new_name": "revenue_by_user",
  "sql": "SELECT u.id, u.name, sum(o.amount) AS revenue FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.id, u.name"
}
```

This runs as a Trino `CREATE TABLE ... AS SELECT` over your tenant's tables — you never
write DDL yourself, and the SELECT is validated by the [SQL guard](../concepts/isolation.md)
first.

## 4. Inspect provenance

`inspect_dataset` returns full metadata — schema, source, tags, row count, and lineage:

```json
{"name": "revenue_by_user"}
```

The `lineage` shows `parents: ["users", "orders"]` and the exact SQL that produced it.
Use `recall_dataset` for a quick look at the rows without writing SQL.

## 5. Export a file

`export_dataset` materializes a dataset (or a query result) to object storage and
returns a **time-limited presigned URL** you can hand to a user:

```json
{"name": "revenue_by_user", "fmt": "csv"}
```

Returns `{uri, presigned_url, format, row_count, size_bytes, expires_in_seconds}`. Share
`presigned_url`; it expires after `MEMCOVE_PRESIGN_TTL_SECONDS` (default 1 hour).

## What you just used

| Step | Tool | Under the hood |
| --- | --- | --- |
| Store | `remember_dataset` | PyIceberg write |
| Query | `query_memory` | SQL guard → Trino, capped preview |
| Derive | `derive_dataset` | guard → Trino CTAS + lineage |
| Inspect | `inspect_dataset` | Postgres registry + Iceberg schema |
| Export | `export_dataset` | Trino → S3 + presigned URL |

For very large data, swap the inline/preview steps for the streaming data plane —
[`open_ingest_stream` and `stream_dataset`](../tools/streaming.md).

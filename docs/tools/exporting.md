# Exporting

Turn a dataset or query result into a downloadable file.

## `export_dataset`

Materialize a dataset or query result to a file in object storage and return a
time-limited **presigned download URL**. Use this when a **user** needs the data as a file
(parquet/csv/json) — not just a preview in the chat.

Provide exactly one of `name` (a whole dataset) or `sql` (a query result). For just looking
at rows yourself, use [`query_memory`](querying.md#query_memory) /
[`recall_dataset`](reading.md#recall_dataset) instead.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `fmt` | `"parquet"` \| `"csv"` \| `"json"` | `"parquet"` | Output file format. |
| `name` | `str` \| `null` | `null` | Export this whole dataset (provide `name` OR `sql`, not both). |
| `sql` | `str` \| `null` | `null` | Export the result of this SELECT (provide `name` OR `sql`, not both). |

**Returns** — `{uri, presigned_url, format, row_count, size_bytes, expires_in_seconds}`.
Share the `presigned_url` with the user; it expires after `MEMCOVE_PRESIGN_TTL_SECONDS`
(default 1 hour).

**Examples**

```json
{"name": "revenue_by_user", "fmt": "csv"}
```
```json
{"sql": "SELECT * FROM orders WHERE amount > 100", "fmt": "parquet"}
```

!!! info "Under the hood"
    The (guarded) SELECT is capped at `MEMCOVE_EXPORT_ROW_CAP` (default 5,000,000),
    pulled from Trino as an Arrow table, serialized to the chosen format, written to the
    artifacts bucket under `exports/{tenant}/...`, and returned as a presigned GET URL.

# Storing data

How data enters Memcove: inline for small data, or an out-of-band upload for large files.

## `remember_dataset`

Persist a table/dataframe into durable memory as a named dataset, so you and future turns
or agents can query and build on it. This is how data **enters** Memcove.

Use it the moment you produce or receive data worth keeping. For a result you only need
once, use [`query_memory`](querying.md#query_memory) and don't persist it. To build a
dataset **from** datasets already in memory, use [`derive_dataset`](querying.md#derive_dataset)
(it records lineage).

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | — | Name to store the dataset under (lowercase letters, digits, underscores). |
| `source` | `dict` | — | Where the data comes from — see the source shapes below. |
| `mode` | `"create"` \| `"replace"` \| `"append"` | `"create"` | create = fail if it exists, replace = overwrite, append = add rows. |
| `tags` | `list[str]` \| `null` | `null` | Optional labels to organize and later filter datasets. |
| `target` | `"lakehouse"` \| `"scratch"` | `"lakehouse"` | Where to store it — `scratch` uses the ephemeral [scratchpad plane](../concepts/scratchpad.md) (inline sources only). |

**`source` shapes**

```json
{"kind": "inline", "format": "json_records", "records": [{"day": "mon", "n": 12}]}
```
```json
{"kind": "inline", "format": "arrow_ipc_b64", "data": "<base64 Arrow IPC>"}
```
```json
{"kind": "s3_parquet", "uri": "s3://bucket/path.parquet"}
```
```json
{"kind": "upload_handle", "handle": "<from start_large_upload>"}
```

- **inline** — send rows directly. Capped at `MEMCOVE_INLINE_BYTES_CAP` (default 8 MiB);
  larger payloads must use an upload or an `s3_parquet` reference.
- **s3_parquet** — reference an existing parquet file. The URI must match
  `MEMCOVE_ALLOWED_S3_INGEST_PREFIXES`, which is **empty (disabled) by default** — this is
  fail-closed to prevent reading arbitrary buckets. See [Settings](../configuration/settings.md).
- **upload_handle** — after a large upload; the handle is bound to your tenant.

**Returns** — the stored dataset's name, schema, and row count.

**Example**

```json
{
  "name": "signups",
  "source": {"kind": "inline", "format": "json_records",
             "records": [{"day": "mon", "n": 12}]}
}
```

!!! info "Under the hood"
    Inline/`s3`/`upload` all resolve to a PyArrow table and are written via
    `catalog.write_arrow` (PyIceberg) — never Trino. Metadata (source, tags, lineage)
    lands in the Postgres registry.

---

## `start_large_upload`

Get a presigned PUT URL for uploading a **large** parquet file out-of-band — for data too
big to send inline through a tool call.

Upload your parquet to the returned URL, then call
[`remember_dataset`](#remember_dataset) with
`source={"kind": "upload_handle", "handle": "<upload_handle>"}`. For small data, skip this
and pass rows inline.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | — | Name you intend to store the uploaded dataset under. |

**Returns** — `{upload_handle, presigned_url, expires_in_seconds}`. The handle is scoped
to your tenant (`uploads/{tenant}/...`); the URL expires after
`MEMCOVE_PRESIGN_TTL_SECONDS` (default 1 hour).

**Flow**

```text
start_large_upload(name)  ->  PUT parquet to presigned_url  ->  remember_dataset(upload_handle)
```

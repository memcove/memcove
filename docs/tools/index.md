# MCP tools overview

Memcove exposes 12 tools and 2 resources over MCP. Every tool operates strictly within
the caller's tenant namespace (resolved from the request headers — see
[Connect an MCP client](../getting-started/connecting.md)).

The descriptions here are the same ones the agent sees at runtime.

## The tools

| Tool | Purpose | Group |
| --- | --- | --- |
| [`remember_dataset`](storing.md#remember_dataset) | Store a table/dataframe/file as a named dataset | Storing |
| [`start_large_upload`](storing.md#start_large_upload) | Get a presigned URL to upload a large parquet out-of-band | Storing |
| [`query_memory`](querying.md#query_memory) | Run a read-only SQL SELECT, get a capped preview | Querying |
| [`derive_dataset`](querying.md#derive_dataset) | Save a SELECT result as a new dataset, with lineage | Querying |
| [`recall_dataset`](reading.md#recall_dataset) | Read one dataset (preview / schema / stats) without SQL | Reading |
| [`inspect_dataset`](reading.md#inspect_dataset) | Full metadata: schema, source, tags, lineage | Reading |
| [`list_memory`](reading.md#list_memory) | List your datasets (optionally filtered by tag) | Reading |
| [`discover_reference_data`](reading.md#discover_reference_data) | List shared read-only reference schemas/tables | Reading |
| [`export_dataset`](exporting.md#export_dataset) | Materialize a dataset/query to a file + presigned URL | Exporting |
| [`stream_dataset`](streaming.md#stream_dataset) | Get an Arrow Flight ticket to stream a large result out | Streaming |
| [`open_ingest_stream`](streaming.md#open_ingest_stream) | Open an Arrow Flight channel to stream a large dataset in | Streaming |
| [`forget_dataset`](deleting.md#forget_dataset) | Permanently delete a dataset | Deleting |

## Resources

| Resource URI | Returns |
| --- | --- |
| `memcove://{tenant}/{name}` | Metadata for a single dataset |
| `memcove://{tenant}/_catalog` | List of all datasets for a tenant |

See [Resources](resources.md).

## How to read these pages

Each tool lists its **parameters** (name, type, default, and the description the agent
sees) and its **return shape**.

!!! info "The tenant is implicit"
    Datasets are always referenced by their **bare name** in SQL and tool arguments. The
    tenant is never a parameter — it comes from the request headers and scopes every call
    automatically. See [Connect an MCP client](../getting-started/connecting.md).

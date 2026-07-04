"""Memcove MCP server (Streamable HTTP).

Exposes the lakehouse as an agent's persistent data memory via MCP tools +
resources. The tenant is resolved from the request header (auth deferred — see
``core/tenancy.py``); every tool operates strictly within that tenant's
namespace (datasets are private per tenant).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from memcove.core import registry
from memcove.core.config import get_settings
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select
from memcove.core.tenancy import normalize_tenant, resolve_tenant
from memcove.data_plane import tickets
from memcove.tools import artifacts as artifacts_tool
from memcove.tools import derive as derive_tool
from memcove.tools import ingest as ingest_tool
from memcove.tools import objects as objects_tool
from memcove.tools import query as query_tool

logger = logging.getLogger("memcove")

INSTRUCTIONS = """\
Memcove is your persistent, queryable data memory. Use it to store tabular data —
dataframes, query results, uploaded files — as named datasets that survive across
turns and agents, then compute over them with SQL instead of holding data in the
conversation.

Typical flow:
  1. remember_dataset (or start_large_upload -> remember_dataset) to store data
  2. query_memory to explore it with SQL
  3. derive_dataset to save computed tables (joins/rollups) with lineage
  4. export_dataset to hand the user a downloadable file
Use list_memory / inspect_dataset to discover and audit what's already stored.

For very large data, use the streaming data plane instead of inline payloads:
open_ingest_stream to stream rows IN, stream_dataset to stream a big result OUT
(both hand back an Arrow Flight endpoint; the bytes bypass this channel).

Reference datasets by their bare name in SQL, e.g. `SELECT * FROM my_dataset`.
Datasets are private to you. Prefer storing data in Memcove over pasting large
tables into the conversation."""

settings = get_settings()
mcp = FastMCP(
    name="memcove",
    instructions=INSTRUCTIONS,
    host=settings.host,
    port=settings.port,
)


def _tenant(ctx: Context) -> str:
    """Resolve the calling tenant from the HTTP request headers."""
    headers: dict[str, str] = {}
    try:
        request = ctx.request_context.request
        if request is not None:
            headers = dict(request.headers)
    except Exception:  # noqa: BLE001 - stdio / no request context
        headers = {}
    return resolve_tenant(headers)


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json")
    return obj


# --------------------------------------------------------------------------- tools


@mcp.tool()
def remember_dataset(
    name: Annotated[
        str, Field(description="Name to store the dataset under (lowercase letters, digits, underscores).")
    ],
    source: Annotated[
        dict,
        Field(description="Where the data comes from — see the source shapes in the description."),
    ],
    ctx: Context,
    mode: Annotated[
        Literal["create", "replace", "append"],
        Field(description="create = fail if it exists, replace = overwrite, append = add rows."),
    ] = "create",
    tags: Annotated[
        list[str] | None, Field(description="Optional labels to organize and later filter datasets.")
    ] = None,
) -> dict:
    """Persist a table/dataframe into durable memory as a named dataset, so you and
    future turns or agents can query and build on it. This is how data ENTERS Memcove.

    Use this the moment you produce or receive data worth keeping (a dataframe you
    built, query results you'll reuse, an uploaded file). For a result you only need
    once, use `query_memory` and don't persist it. To build a dataset FROM datasets
    already in memory, use `derive_dataset` (it records lineage).

    `source` is one of:
      • {"kind":"inline","format":"json_records","records":[{...}, ...]}    small data, sent directly
      • {"kind":"inline","format":"arrow_ipc_b64","data":"<base64 Arrow IPC>"}
      • {"kind":"s3_parquet","uri":"s3://bucket/path.parquet"}             reference existing parquet
      • {"kind":"upload_handle","handle":"<from start_large_upload>"}       after a large upload

    Returns the stored dataset's name, schema, and row count.
    Example: remember_dataset(name="signups",
      source={"kind":"inline","format":"json_records","records":[{"day":"mon","n":12}]})
    """
    return _dump(ingest_tool.ingest_object(_tenant(ctx), name, source, mode=mode, tags=tags))


@mcp.tool()
def query_memory(
    sql: Annotated[
        str,
        Field(description="A read-only SQL SELECT. Reference datasets by their bare name."),
    ],
    ctx: Context,
    limit: Annotated[
        int | None, Field(description="Max rows to return in the preview.")
    ] = None,
) -> dict:
    """Run a read-only SQL SELECT over your datasets and get a preview of the rows
    back (capped). This is the main way to ASK QUESTIONS of stored data — filters,
    joins, aggregations, anything SELECT.

    Reference datasets by their bare name, e.g.
    `SELECT region, count(*) FROM signups GROUP BY region`.
    Only read queries are allowed (SELECT / WITH / UNION); it cannot modify data.
      • To SAVE a result as a new reusable dataset, use `derive_dataset`.
      • To hand the full result to a user as a file, use `export_dataset`.
      • To dump a whole dataset without writing SQL, use `recall_dataset`.

    Returns {columns, rows, row_count, truncated}. `truncated` is true if more rows
    exist beyond the cap — narrow the query or use `export_dataset` for everything.
    """
    return _dump(query_tool.run_query(_tenant(ctx), sql, limit=limit))


@mcp.tool()
def derive_dataset(
    new_name: Annotated[str, Field(description="Name for the new dataset to create.")],
    sql: Annotated[
        str,
        Field(description="A read-only SELECT over existing datasets, referenced by bare name."),
    ],
    ctx: Context,
    mode: Annotated[
        Literal["create", "replace"],
        Field(description="create = fail if it exists, replace = overwrite."),
    ] = "create",
    tags: Annotated[
        list[str] | None, Field(description="Optional labels to organize and later filter datasets.")
    ] = None,
) -> dict:
    """Create a NEW named dataset from a SQL SELECT over existing datasets and persist
    it — a join, rollup, or filtered view you want to keep and reuse. Lineage back to
    the source datasets is recorded automatically (visible via `inspect_dataset`).

    Use this instead of `query_memory` when the result is worth keeping. Use
    `remember_dataset` instead when the data comes from OUTSIDE (inline/file/upload)
    rather than from a query.

    Returns the new dataset's name, schema, row count, and lineage.
    Example: derive_dataset(new_name="revenue_by_user",
      sql="SELECT u.id, sum(o.amount) AS revenue FROM users u "
          "JOIN orders o ON o.user_id = u.id GROUP BY u.id")
    """
    return _dump(derive_tool.derive_object(_tenant(ctx), new_name, sql, mode=mode, tags=tags))


@mcp.tool()
def recall_dataset(
    name: Annotated[str, Field(description="Name of the dataset to read.")],
    ctx: Context,
    mode: Annotated[
        Literal["preview", "schema", "stats"],
        Field(description="preview = first rows, schema = columns/types, stats = row count + schema."),
    ] = "preview",
    limit: Annotated[int | None, Field(description="Max rows when mode=preview.")] = None,
) -> dict:
    """Read a named dataset directly, without writing SQL.

    `mode`: "preview" (first rows, capped), "schema" (column names/types), or
    "stats" (row count + schema). Use this for a quick look at one dataset. To
    filter/join/aggregate, use `query_memory`; for provenance/lineage use
    `inspect_dataset`.
    """
    return _dump(objects_tool.get_object(_tenant(ctx), name, mode=mode, limit=limit))


@mcp.tool()
def inspect_dataset(
    name: Annotated[str, Field(description="Name of the dataset to inspect.")],
    ctx: Context,
) -> dict:
    """Get full metadata for a dataset: schema, where it came from (source), tags,
    row count, and its LINEAGE — which datasets and SQL produced it.

    Use this to understand or audit a dataset before trusting or building on it. For
    the actual rows, use `recall_dataset` or `query_memory`.
    """
    return _dump(objects_tool.describe_object(_tenant(ctx), name))


@mcp.tool()
def list_memory(
    ctx: Context,
    tags: Annotated[
        list[str] | None, Field(description="Only return datasets carrying any of these tags.")
    ] = None,
) -> dict:
    """List the datasets currently in your memory (name, source, tags).

    Start here to discover what's already stored before querying or re-ingesting —
    it avoids duplicating data you already have. Optionally filter by `tags`, then
    use `inspect_dataset` / `recall_dataset` to dig into one.
    """
    return {"datasets": objects_tool.list_objects(_tenant(ctx), tags)}


@mcp.tool()
def export_dataset(
    ctx: Context,
    fmt: Annotated[
        Literal["parquet", "csv", "json"], Field(description="Output file format.")
    ] = "parquet",
    name: Annotated[
        str | None, Field(description="Export this whole dataset (provide name OR sql, not both).")
    ] = None,
    sql: Annotated[
        str | None, Field(description="Export the result of this SELECT (provide name OR sql, not both).")
    ] = None,
) -> dict:
    """Materialize a dataset or query result to a file in object storage and return a
    time-limited presigned download URL. Use this when a USER needs the data as a
    file (parquet/csv/json) — not just a preview in the chat.

    Provide exactly one of `name` (a whole dataset) or `sql` (a query result). For
    just looking at rows yourself, use `query_memory` / `recall_dataset` instead.

    Returns {uri, presigned_url, format, row_count, size_bytes, expires_in_seconds}.
    Share the `presigned_url` with the user; it expires.
    """
    return _dump(artifacts_tool.export_artifact(_tenant(ctx), fmt=fmt, label=name, sql=sql))


@mcp.tool()
def start_large_upload(
    name: Annotated[str, Field(description="Name you intend to store the uploaded dataset under.")],
    ctx: Context,
) -> dict:
    """Get a presigned PUT URL for uploading a LARGE parquet file out-of-band — for
    data too big to send inline through a tool call.

    Upload your parquet to the returned URL, then call `remember_dataset` with
    source={"kind":"upload_handle","handle":"<upload_handle>"}. For small data, skip
    this and pass the rows inline to `remember_dataset` directly.

    Returns {upload_handle, presigned_url, expires_in_seconds}.
    """
    return _dump(ingest_tool.request_upload(_tenant(ctx), name))


@mcp.tool()
def forget_dataset(
    name: Annotated[str, Field(description="Name of the dataset to permanently delete.")],
    ctx: Context,
) -> dict:
    """Permanently delete a dataset from memory (drops the underlying table and its
    metadata). This CANNOT be undone, and datasets derived from it will lose their
    source.

    Only use when explicitly asked to remove data. To overwrite a dataset with new
    contents instead, use `remember_dataset` / `derive_dataset` with mode="replace".
    """
    result = objects_tool.drop_object(_tenant(ctx), name)
    return {"forgotten": result.get("dropped", name)}


@mcp.tool()
def stream_dataset(
    ctx: Context,
    name: Annotated[
        str | None, Field(description="Stream this whole dataset (provide name OR sql, not both).")
    ] = None,
    sql: Annotated[
        str | None, Field(description="Stream the result of this SELECT (provide name OR sql, not both).")
    ] = None,
) -> dict:
    """Get an Arrow Flight ticket to STREAM a large result back as Arrow batches,
    for when the data is too big for a `query_memory` preview or you want it as
    live Arrow rather than a file.

    This is the read side of the streaming data plane. It returns an endpoint +
    ticket; an Arrow Flight client then calls DoGet(ticket) to pull the batches —
    the bytes never pass through this MCP channel. Provide exactly one of `name`
    or `sql`. For a quick look use `query_memory`; for a downloadable file use
    `export_dataset`.

    Returns {flight_uri, transport:"arrow-flight", ticket_b64, how}. Decode
    ticket_b64 from base64 to get the raw DoGet ticket bytes.
    """
    if bool(name) == bool(sql):
        raise ValueError("provide exactly one of 'name' or 'sql'")
    settings = get_settings()
    tenant = _tenant(ctx)
    if name:
        cmd = tickets.read_command(tenant, validate_label(name))
    else:
        validate_select(sql, tenant_ns=tenant, catalog=settings.trino_catalog)  # fail fast
        cmd = tickets.query_command(tenant, sql)
    return {
        "flight_uri": settings.flight_advertise_uri,
        "transport": "arrow-flight",
        "ticket_b64": tickets.to_b64(cmd),
        "how": "DoGet on flight_uri with base64-decoded ticket_b64 to stream Arrow batches",
    }


@mcp.tool()
def open_ingest_stream(
    name: Annotated[str, Field(description="Name to store the streamed dataset under.")],
    ctx: Context,
    mode: Annotated[
        Literal["create", "replace", "append"],
        Field(description="create = fail if it exists, replace = overwrite, append = add rows."),
    ] = "create",
) -> dict:
    """Open an Arrow Flight channel to STREAM a large dataset IN as Arrow batches,
    for data too big to send inline to `remember_dataset`.

    This is the write side of the streaming data plane. It returns an endpoint +
    a DoPut descriptor command; an Arrow Flight client then calls DoPut with that
    descriptor and writes record batches — the bytes never pass through this MCP
    channel. For small data, just use `remember_dataset` inline; for a parquet file
    you already have, `start_large_upload` is simpler.

    Returns {flight_uri, transport:"arrow-flight", descriptor_command_b64, how}.
    Decode descriptor_command_b64 and pass it to FlightDescriptor.for_command(...).
    """
    settings = get_settings()
    cmd = tickets.ingest_command(_tenant(ctx), validate_label(name), mode)
    return {
        "flight_uri": settings.flight_advertise_uri,
        "transport": "arrow-flight",
        "descriptor_command_b64": tickets.to_b64(cmd),
        "how": "DoPut on flight_uri using FlightDescriptor.for_command(base64-decoded), then write Arrow batches",
    }


# ----------------------------------------------------------------------- resources


@mcp.resource("memcove://{tenant}/{name}")
def dataset_resource(tenant: str, name: str) -> dict:
    """Metadata for a single dataset (schema, source, tags, lineage)."""
    return _dump(objects_tool.describe_object(normalize_tenant(tenant), name))


@mcp.resource("memcove://{tenant}/_catalog")
def catalog_resource(tenant: str) -> dict:
    """List all datasets for a tenant."""
    return {"datasets": objects_tool.list_objects(normalize_tenant(tenant))}


# ---------------------------------------------------------------------------- main


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        registry.init_db()
        logger.info("registry initialized")
    except Exception as exc:  # noqa: BLE001
        logger.warning("registry init failed (is postgres up?): %s", exc)
    logger.info("starting Memcove MCP server on %s:%s", settings.host, settings.port)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

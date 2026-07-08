"""Memcove MCP server (Streamable HTTP).

Exposes the lakehouse as an agent's persistent data memory via MCP tools +
resources. The tenant is resolved from the request header (auth deferred — see
``core/tenancy.py``); every tool operates strictly within that tenant's
namespace (datasets are private per tenant).
"""

from __future__ import annotations

import logging
import socket
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from memcove.core import registry
from memcove.core.config import get_settings
from memcove.core.errors import TenancyError
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select
from memcove.core.tenancy import normalize_tenant, resolve_tenant, resolve_tenant_from_claims
from memcove.data_plane import tickets
from memcove.tools import artifacts as artifacts_tool
from memcove.tools import derive as derive_tool
from memcove.tools import discovery as discovery_tool
from memcove.tools import ingest as ingest_tool
from memcove.tools import objects as objects_tool
from memcove.tools import query as query_tool

logger = logging.getLogger("memcove")

INSTRUCTIONS = """\
Memcove is your cross-source join engine and persistent data memory. When two
tools or sources can't see each other, land both here as named datasets and join
them in one SQL query. Datasets survive across turns and agents, so you compute
over stored tables with SQL instead of holding data in the conversation.

Reach for Memcove when:
  • you must JOIN or AGGREGATE across sources that can't query each other
  • one tool's output has to be matched against another tool's output
  • a result is worth reusing across turns/agents (don't re-fetch or re-paste it)
For a value you need only once, compute it and move on — don't persist it.

Discover first (safe opening move; also confirms the backend is reachable):
  1. list_memory — what datasets do you already have? (avoids re-ingesting)
  2. discover_reference_data — the source may already live in shared reference
     data (e.g. ref_market.*), so you can join it without shuttling anything in
Then bring in what's missing and compute:
  3. remember_dataset — land external data as a named dataset (recipe below)
  4. query_memory — join / filter / aggregate across all of them with SQL
  5. derive_dataset — save a computed join/rollup, with lineage, to reuse
  6. export_dataset — hand the user a downloadable file

Getting a source IN (the bridge):
  • small (within the inline cap): remember_dataset(source={"kind":"inline",...})
  • large extract: have the source UNLOAD/export parquet to S3, then
    remember_dataset(source={"kind":"s3_parquet","uri":"s3://…"})
  • a parquet file in hand: start_large_upload -> PUT the file ->
    remember_dataset(source={"kind":"upload_handle",...})
  • huge / continuous: open_ingest_stream (Arrow Flight; bytes bypass this channel)

Prerequisites & limits: durable writes need server-side S3 credentials; inline
payloads are size-capped (go S3/upload above it); s3_parquet ingest works only
for operator-allowlisted buckets; the fast scratch plane (target="scratch") is
off unless the operator enabled it; reference schemas are read-only.

If a write fails: a credential/access error is the server-side S3 backend, not
your call — fall back to target="scratch" or a smaller inline payload. If every
write path fails, tell the user Memcove is unavailable and finish the task another
way rather than retrying blindly.

Reference your datasets by bare name (`SELECT * FROM my_dataset`) and reference-
plane tables by qualified name (`ref_market.prices`). Your datasets are private
to you. Prefer joining in Memcove over pasting large tables into the conversation."""

settings = get_settings()


def _build_auth_kwargs(s) -> dict:
    """FastMCP auth kwargs for native OAuth resource-server mode, or {} for header mode."""
    if not s.oauth_enabled:
        return {}
    from mcp.server.auth.settings import AuthSettings

    from memcove.core.oauth import build_token_verifier

    base_url = s.public_url or f"http://{s.host}:{s.port}"
    return {
        "token_verifier": build_token_verifier(s),
        "auth": AuthSettings(
            issuer_url=s.oauth_issuer,
            resource_server_url=base_url,
            required_scopes=s.oauth_required_scopes or None,
        ),
    }


mcp = FastMCP(
    name="memcove",
    instructions=INSTRUCTIONS,
    host=settings.host,
    port=settings.port,
    **_build_auth_kwargs(settings),
)


def _tenant(ctx: Context) -> str:
    """Resolve the calling tenant.

    In native OAuth mode the tenant comes from the verified bearer token's claims; the
    SDK's auth middleware has already rejected unauthenticated requests. Otherwise it's
    resolved from the request headers set by the auth proxy (the trusted-header model).
    """
    if get_settings().oauth_enabled:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or token.claims is None:
            raise TenancyError("unauthenticated request")
        return resolve_tenant_from_claims(token.claims)

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
    target: Annotated[
        Literal["lakehouse", "scratch"],
        Field(description="lakehouse = durable (default); scratch = fast, small, ephemeral (inline only)."),
    ] = "lakehouse",
) -> dict:
    """Persist a table/dataframe into durable memory as a named dataset — how a
    source ENTERS the join fabric, so you and future turns or agents can query it
    and JOIN it against other landed datasets.

    Use this the moment you produce or receive data worth keeping (a dataframe you
    built, query results you'll reuse, an uploaded file). For a result you only need
    once, use `query_memory` and don't persist it. To build a dataset FROM datasets
    already in memory, use `derive_dataset` (it records lineage).

    `source` is one of:
      • {"kind":"inline","format":"json_records","records":[{...}, ...]}    small data, sent directly
      • {"kind":"inline","format":"arrow_ipc_b64","data":"<base64 Arrow IPC>"}
      • {"kind":"s3_parquet","uri":"s3://bucket/path.parquet"}             reference existing parquet
      • {"kind":"upload_handle","handle":"<from start_large_upload>"}       after a large upload

    Limits: `inline` payloads are size-capped — above the cap, have the source
    export parquet to S3 and use `s3_parquet`, or `start_large_upload` +
    `upload_handle`. `s3_parquet` only reads operator-allowlisted bucket prefixes
    (disabled otherwise). `target="scratch"` is the fast ephemeral plane — it takes
    inline sources only and must be enabled by the operator; a credential/access
    error on a durable write is the server-side S3 backend, so retrying inline or
    scratch is the fallback, not repeating the same call.

    Returns the stored dataset's name, schema, and row count.
    Example: remember_dataset(name="signups",
      source={"kind":"inline","format":"json_records","records":[{"day":"mon","n":12}]})
    """
    return _dump(
        ingest_tool.ingest_object(_tenant(ctx), name, source, mode=mode, tags=tags, target=target)
    )


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

    This is where cross-source joins happen: reference every landed dataset by its
    bare name and JOIN them in one SELECT, even if the sources they came from could
    never query each other. Shared reference tables join in by qualified name too
    (e.g. `ref_market.prices`).

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
    target: Annotated[
        Literal["lakehouse", "scratch"],
        Field(description="lakehouse = durable, with lineage (default); scratch = fast, small, ephemeral."),
    ] = "lakehouse",
) -> dict:
    """Create a NEW named dataset from a SQL SELECT over existing datasets and persist
    it — a join, rollup, or filtered view you want to keep and reuse. Lineage back to
    the source datasets is recorded automatically (visible via `inspect_dataset`).

    Set `target="scratch"` to materialize into the fast, ephemeral scratchpad instead of
    durable memory. Scratch datasets are addressed as `scratch.<name>` in later SQL and
    can be JOINed with durable datasets in one `query_memory` call.

    Use this instead of `query_memory` when the result is worth keeping. Use
    `remember_dataset` instead when the data comes from OUTSIDE (inline/file/upload)
    rather than from a query.

    Returns the new dataset's name, schema, row count, and lineage.
    Example: derive_dataset(new_name="revenue_by_user",
      sql="SELECT u.id, sum(o.amount) AS revenue FROM users u "
          "JOIN orders o ON o.user_id = u.id GROUP BY u.id")
    """
    return _dump(
        derive_tool.derive_object(_tenant(ctx), new_name, sql, mode=mode, tags=tags, target=target)
    )


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
def discover_reference_data(ctx: Context) -> dict:
    """List the shared reference datasets available to every tenant (read-only).

    Beyond your own private datasets, Memcove may expose shared reference data
    (e.g. market/reference tables) that anyone can query but no one can modify.
    Check here before ingesting: a source you were about to shuttle in may already
    live here read-only, ready to JOIN — no bridging needed. Use this to see which
    shared schemas and tables exist and their columns, then read them in SQL by
    their qualified name, e.g. `SELECT * FROM ref_market.prices`.

    Returns {schemas: [{schema, tables: [{name, columns: [{name, type}]}]}]}.
    """
    _ = _tenant(ctx)  # ensure the caller is a resolvable tenant before disclosing
    return discovery_tool.discover_reference_data()


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
        validate_select(  # fail fast
            sql, tenant_ns=tenant, catalog=settings.trino_catalog,
            shared_schemas=settings.shared_schemas,
        )
        cmd = tickets.query_command(tenant, sql)
    return {
        "flight_uri": settings.flight_advertise_uri,
        "transport": "arrow-flight",
        "ticket_b64": tickets.to_b64_signed(cmd),
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
        "descriptor_command_b64": tickets.to_b64_signed(cmd),
        "how": "DoPut on flight_uri using FlightDescriptor.for_command(base64-decoded), then write Arrow batches",
    }


# ----------------------------------------------------------------------- resources


def _authorized_tenant(ctx: Context, uri_tenant: str) -> str:
    """Resolve the caller's tenant and require the URI's tenant to match it.

    Resources are addressed as ``memcove://{tenant}/…``; without this check any caller
    could read another tenant's metadata by naming it in the URI. The tenant is decided
    by the caller's verified identity (token/proxy header), never by the URI alone.
    """
    caller = _tenant(ctx)
    if normalize_tenant(uri_tenant) != caller:
        raise TenancyError("cannot access another tenant's resources")
    return caller


@mcp.resource("memcove://{tenant}/{name}")
def dataset_resource(tenant: str, name: str, ctx: Context) -> dict:
    """Metadata for a single dataset (schema, source, tags, lineage)."""
    return _dump(objects_tool.describe_object(_authorized_tenant(ctx, tenant), name))


@mcp.resource("memcove://{tenant}/_catalog")
def catalog_resource(tenant: str, ctx: Context) -> dict:
    """List all datasets for a tenant."""
    return {"datasets": objects_tool.list_objects(_authorized_tenant(ctx, tenant))}


# ------------------------------------------------------------------- health probes


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> PlainTextResponse:
    """Liveness: the process is up and serving HTTP. No dependency checks, so a
    slow/down backend never triggers a pod restart."""
    return PlainTextResponse("ok")


def _trino_reachable() -> bool:
    try:
        with socket.create_connection((settings.trino_host, settings.trino_port), timeout=2):
            return True
    except OSError:
        return False


@mcp.custom_route("/ready", methods=["GET"])
async def ready(_request: Request) -> JSONResponse:
    """Readiness: the dependencies the control plane needs are reachable.

    Checks the metadata registry (a real ``SELECT 1``) and Trino (socket reach).
    Returns 503 with per-check detail if anything is down so k8s holds traffic
    off the pod until it can actually serve. Blocking checks run in a thread so
    the event loop is never stalled.
    """
    checks: dict[str, str] = {}

    try:
        await run_in_threadpool(registry.ping)
        checks["registry"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report any failure as not-ready
        checks["registry"] = f"error: {exc}"

    checks["trino"] = "ok" if await run_in_threadpool(_trino_reachable) else "unreachable"

    is_ready = all(v == "ok" for v in checks.values())
    return JSONResponse({"ready": is_ready, "checks": checks}, status_code=200 if is_ready else 503)


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

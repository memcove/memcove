"""Ingest tool — inline data, s3 parquet reference, or finalized upload."""

from __future__ import annotations

import uuid

import pyarrow as pa

from memcove.core import arrow_io, catalog, registry, scratch, storage
from memcove.core.config import get_settings
from memcove.core.errors import IngestError, ObjectExistsError
from memcove.core.models import MemoryObject, SourceKind, UploadTicket
from memcove.core.naming import validate_label
from memcove.tools.objects import describe_object, pending_object


def _trino_type(at: pa.DataType) -> str:
    """Arrow type -> the Trino type to CAST a scratch column to."""
    if pa.types.is_boolean(at):
        return "BOOLEAN"
    if pa.types.is_integer(at):
        return "BIGINT"
    if pa.types.is_floating(at):
        return "DOUBLE"
    if pa.types.is_timestamp(at):
        return "TIMESTAMP"
    if pa.types.is_date(at):
        return "DATE"
    return "VARCHAR"


def _literal(v: object, at: pa.DataType) -> str:
    """A Trino SQL literal for one cell; the enclosing CAST enforces the real type."""
    if v is None:
        return "NULL"
    if pa.types.is_boolean(at):
        return "true" if v else "false"
    if pa.types.is_integer(at) or pa.types.is_floating(at):
        return repr(v) if pa.types.is_floating(at) else str(int(v))
    # dates/timestamps arrive as python date/datetime; str() gives an ISO form Trino casts.
    return "'" + str(v).replace("'", "''") + "'"


def _scratch_select(table: pa.Table) -> str:
    """Build a typed SELECT (over a VALUES list) to CTAS an inline table into scratch."""
    fields = list(table.schema)
    cols = ", ".join(
        f'CAST(c{i} AS {_trino_type(f.type)}) AS "{f.name}"' for i, f in enumerate(fields)
    )
    names = ", ".join(f"c{i}" for i in range(len(fields)))
    if table.num_rows == 0:
        # No rows -> a zero-row, correctly-typed shell (VALUES needs >= 1 row).
        nulls = ", ".join("NULL" for _ in fields)
        return f"SELECT {cols} FROM (VALUES ({nulls})) AS _v({names}) WHERE false"
    rows = []
    for row in table.to_pylist():
        vals = ", ".join(_literal(row[f.name], f.type) for f in fields)
        rows.append(f"({vals})")
    return f"SELECT {cols} FROM (VALUES {', '.join(rows)}) AS _v({names})"


def _check_s3_ingest_allowed(uri: str, settings) -> None:
    """Guard against a confused-deputy read of arbitrary S3 via agent-supplied URIs.

    An agent controls ``uri``; without an allowlist Memcove would read any object its
    service credential can reach. Empty allowlist = fail closed (feature disabled).
    """
    prefixes = settings.allowed_s3_ingest_prefixes or []
    if not prefixes:
        raise IngestError(
            "s3_parquet ingest is disabled; set MEMCOVE_ALLOWED_S3_INGEST_PREFIXES "
            "to an allowlist of permitted s3:// prefixes to enable it"
        )

    def _within(prefix: str) -> bool:
        # Match on a path boundary so prefix "s3://bucket" does NOT also permit
        # "s3://bucket-evil"; the uri must equal the prefix or sit under its "/".
        prefix = prefix.rstrip("/")
        return uri == prefix or uri.startswith(prefix + "/")

    if not any(_within(p) for p in prefixes):
        raise IngestError(
            f"s3 uri {uri!r} is not within an allowed ingest prefix; "
            "permitted prefixes are configured by the operator"
        )


def _table_from_source(source: dict, tenant: str) -> tuple[pa.Table, SourceKind, str | None]:
    """Resolve an ingest source descriptor into (arrow_table, source_kind, ref)."""
    kind = source.get("kind")
    settings = get_settings()

    if kind == "inline":
        fmt = source.get("format", "json_records")
        if fmt == "json_records":
            table = arrow_io.from_json_records(source.get("records") or source.get("data") or [])
        elif fmt == "arrow_ipc_b64":
            table = arrow_io.from_arrow_ipc_b64(source["data"])
        else:
            raise IngestError(f"unknown inline format {fmt!r}")
        if arrow_io.estimate_bytes(table) > settings.inline_bytes_cap:
            raise IngestError(
                f"inline payload exceeds cap ({settings.inline_bytes_cap} bytes); "
                "use request_upload + upload_handle or an s3_parquet reference"
            )
        return table, SourceKind.INLINE, None

    if kind == "s3_parquet":
        uri = source["uri"]
        _check_s3_ingest_allowed(uri, settings)
        table = storage.read_parquet_table(uri)
        return table, SourceKind.S3_PARQUET, uri

    if kind == "upload_handle":
        handle = source["handle"]
        # Bind the handle to the caller: minted handles are <prefix>/uploads/{tenant}/...
        # (see request_upload). Without this a caller could read another tenant's
        # pending upload out of the shared staging bucket. The expected key includes any
        # configured bucket sub-path, so a handle for another prefix/tenant is rejected.
        bucket, expected = storage.resolve(settings.staging_bucket, "uploads", tenant)
        if not handle.startswith(expected + "/"):
            raise IngestError("upload handle does not belong to this tenant")
        table = storage.read_parquet_table(bucket, handle)
        return table, SourceKind.UPLOAD, handle

    raise IngestError(f"unknown ingest source kind {kind!r}")


def ingest_object(
    tenant: str,
    label: str,
    source: dict,
    mode: str = "create",
    tags: list[str] | None = None,
    target: str = "lakehouse",
) -> MemoryObject:
    """Ingest data into a labeled object.

    ``target='lakehouse'`` (default) writes a durable Iceberg table via PyIceberg.
    ``target='scratch'`` writes into the ephemeral DuckDB scratchpad (via Trino) — inline
    sources only, since scratch is for small, fast, throwaway data.
    """
    label = validate_label(label)
    if target not in ("lakehouse", "scratch"):
        raise ValueError(f"unknown target {target!r}; expected lakehouse|scratch")
    table, kind, ref = _table_from_source(source, tenant)

    if target == "scratch":
        return _ingest_to_scratch(tenant, label, table, kind, mode)

    catalog.write_arrow(tenant, label, table, mode=mode)
    ok = registry.record_object_guarded(
        tenant,
        label,
        table_ident=f"{get_settings().trino_catalog}.{tenant}.{label}",
        source=kind.value,
        source_ref=ref,
        tags=tags or [],
    )
    if not ok:
        # Data is committed and queryable; only the registry write failed. Return a
        # metadata_pending response built from values in hand (the drift signal is
        # already logged and the reconciler / read-repair will backfill).
        return pending_object(tenant, label, source=kind, source_ref=ref, tags=tags or [])
    return describe_object(tenant, label)


def _ingest_to_scratch(
    tenant: str, label: str, table: pa.Table, kind: SourceKind, mode: str
) -> MemoryObject:
    """Write a small inline table into the DuckDB scratchpad via a Trino VALUES CTAS."""
    if kind is not SourceKind.INLINE:
        raise IngestError(
            "scratch ingest supports inline sources only; use target=lakehouse for "
            "s3_parquet / upload_handle, or derive into scratch from a query"
        )
    if mode not in ("create", "replace"):
        raise IngestError(
            f"scratch ingest supports mode create|replace only, not {mode!r}"
        )
    if mode == "create" and scratch.table_exists(tenant, label):
        raise ObjectExistsError(f"scratch object '{label}' already exists (use mode=replace)")
    scratch.create_as_select(
        tenant, label, _scratch_select(table), replace=(mode == "replace")
    )
    return scratch.describe(tenant, label)


def request_upload(tenant: str, label: str) -> UploadTicket:
    """Hand back a presigned PUT URL for out-of-band parquet upload."""
    label = validate_label(label)
    settings = get_settings()
    bucket, handle = storage.resolve(
        settings.staging_bucket, "uploads", tenant, f"{label}-{uuid.uuid4().hex}.parquet"
    )
    url = storage.presign_put(bucket, handle, content_type="application/octet-stream")
    return UploadTicket(
        upload_handle=handle,
        presigned_url=url,
        expires_in_seconds=settings.presign_ttl_seconds,
    )

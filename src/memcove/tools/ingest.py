"""Ingest tool — inline data, s3 parquet reference, or finalized upload."""

from __future__ import annotations

import uuid

import pyarrow as pa

from memcove.core import arrow_io, catalog, registry, storage
from memcove.core.config import get_settings
from memcove.core.errors import IngestError
from memcove.core.models import MemoryObject, SourceKind, UploadTicket
from memcove.core.naming import validate_label
from memcove.tools.objects import describe_object


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
        # Bind the handle to the caller: minted handles are uploads/{tenant}/...
        # (see request_upload). Without this a caller could read another tenant's
        # pending upload out of the shared staging bucket.
        if not handle.startswith(f"uploads/{tenant}/"):
            raise IngestError("upload handle does not belong to this tenant")
        table = storage.read_parquet_table(settings.staging_bucket, handle)
        return table, SourceKind.UPLOAD, handle

    raise IngestError(f"unknown ingest source kind {kind!r}")


def ingest_object(
    tenant: str,
    label: str,
    source: dict,
    mode: str = "create",
    tags: list[str] | None = None,
) -> MemoryObject:
    """Ingest data into a labeled Iceberg object via the PyIceberg write path."""
    label = validate_label(label)
    table, kind, ref = _table_from_source(source, tenant)

    catalog.write_arrow(tenant, label, table, mode=mode)
    registry.record_object(
        tenant,
        label,
        table_ident=f"{get_settings().trino_catalog}.{tenant}.{label}",
        source=kind.value,
        source_ref=ref,
        tags=tags or [],
    )
    return describe_object(tenant, label)


def request_upload(tenant: str, label: str) -> UploadTicket:
    """Hand back a presigned PUT URL for out-of-band parquet upload."""
    label = validate_label(label)
    settings = get_settings()
    handle = f"uploads/{tenant}/{label}-{uuid.uuid4().hex}.parquet"
    url = storage.presign_put(settings.staging_bucket, handle, content_type="application/octet-stream")
    return UploadTicket(
        upload_handle=handle,
        presigned_url=url,
        expires_in_seconds=settings.presign_ttl_seconds,
    )

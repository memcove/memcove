"""Artifact export — materialize a query/object to S3 and return a presigned URL."""

from __future__ import annotations

import uuid

import orjson
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from memcove.core import storage, trino_client
from memcove.core.audit import audit
from memcove.core.config import get_settings
from memcove.core.errors import MemcoveError
from memcove.core.models import ArtifactRef
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select

_EXT = {"parquet": "parquet", "csv": "csv", "json": "json"}


def export_artifact(
    tenant: str,
    fmt: str = "parquet",
    label: str | None = None,
    sql: str | None = None,
) -> ArtifactRef:
    """Export an object (by label) or a query result to object storage."""
    if fmt not in _EXT:
        raise MemcoveError(f"unsupported format {fmt!r}; expected parquet|csv|json")
    if bool(label) == bool(sql):
        raise MemcoveError("provide exactly one of 'label' or 'sql'")

    settings = get_settings()
    base = "query"
    if label:
        base = validate_label(label)
        sql = f"SELECT * FROM {base}"
    guard = validate_select(
        sql, tenant_ns=tenant, catalog=settings.trino_catalog,
        shared_schemas=settings.shared_schemas,
    )

    capped = f"SELECT * FROM (\n{guard.sql}\n) AS _e LIMIT {settings.export_row_cap}"
    # Stream the cursor batch-by-batch straight into a single object in S3, so peak
    # memory is ~one batch instead of the whole (up to export_row_cap-row) result.
    schema, batches = trino_client.stream_arrow_batches(capped, run_as=tenant)

    bucket, key = storage.resolve(
        settings.artifacts_bucket, "exports", tenant, f"{base}-{uuid.uuid4().hex}.{_EXT[fmt]}"
    )

    if fmt == "parquet":
        row_count = _stream_parquet(bucket, key, schema, batches)
    elif fmt == "csv":
        row_count = _stream_csv(bucket, key, schema, batches)
    else:  # json
        row_count = _stream_json(bucket, key, batches)

    size = storage.object_size(bucket, key)
    audit("export", tenant=tenant, fmt=fmt, rows=row_count, key=key)
    return ArtifactRef(
        uri=storage.s3_uri(bucket, key),
        presigned_url=storage.presign_get(bucket, key),
        format=fmt,
        row_count=row_count,
        size_bytes=size,
        expires_in_seconds=settings.presign_ttl_seconds,
    )


def _stream_parquet(bucket, key, schema, batches) -> int:
    row_count = 0
    with storage.open_output_stream(bucket, key) as sink, pq.ParquetWriter(sink, schema) as w:
        for b in batches:
            w.write_batch(b)
            row_count += b.num_rows
    return row_count


def _stream_csv(bucket, key, schema, batches) -> int:
    row_count = 0
    with storage.open_output_stream(bucket, key) as sink:
        # CSVWriter emits the header on construction, so a zero-row export still
        # yields a valid header-only file.
        writer = pacsv.CSVWriter(sink, schema)
        try:
            for b in batches:
                writer.write_batch(b)
                row_count += b.num_rows
        finally:
            writer.close()
    return row_count


def _stream_json(bucket, key, batches) -> int:
    """Stream a JSON array, one batch's rows resident at a time."""
    row_count = 0
    first = True
    with storage.open_output_stream(bucket, key) as sink:
        sink.write(b"[")
        for b in batches:
            inner = orjson.dumps(b.to_pylist(), default=str)[1:-1]  # strip outer [ ]
            if inner:
                sink.write(inner if first else b"," + inner)
                first = False
            row_count += b.num_rows
        sink.write(b"]")
    return row_count

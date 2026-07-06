"""Artifact export — materialize a query/object to S3 and return a presigned URL."""

from __future__ import annotations

import io
import uuid

import orjson
import pyarrow.csv as pacsv

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
    table = trino_client.execute_arrow(capped, run_as=tenant)

    key = f"exports/{tenant}/{base}-{uuid.uuid4().hex}.{_EXT[fmt]}"
    bucket = settings.artifacts_bucket

    if fmt == "parquet":
        size = storage.write_parquet_table(table, bucket, key)
        content_type = "application/vnd.apache.parquet"
    elif fmt == "csv":
        buf = io.BytesIO()
        pacsv.write_csv(table, buf)
        size = storage.write_bytes(bucket, key, buf.getvalue(), "text/csv")
        content_type = "text/csv"
    else:  # json
        payload = orjson.dumps(table.to_pylist(), default=str)
        size = storage.write_bytes(bucket, key, payload, "application/json")
        content_type = "application/json"

    _ = content_type  # reserved for future metadata
    audit("export", tenant=tenant, fmt=fmt, rows=table.num_rows, key=key)
    return ArtifactRef(
        uri=storage.s3_uri(bucket, key),
        presigned_url=storage.presign_get(bucket, key),
        format=fmt,
        row_count=table.num_rows,
        size_bytes=size,
        expires_in_seconds=settings.presign_ttl_seconds,
    )

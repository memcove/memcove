"""Object-store (S3/MinIO) helpers for the data plane.

Owns presigned URLs (upload + artifact download), the staging area for
out-of-band uploads, and reading/writing parquet objects with PyArrow.
"""

from __future__ import annotations

import io
from functools import lru_cache

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.client import Config

from memcove.core.config import Settings, get_settings


@lru_cache
def _client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if s.s3_path_style else "auto"},
        ),
    )


def _settings() -> Settings:
    return get_settings()


def presign_put(bucket: str, key: str, content_type: str = "application/octet-stream") -> str:
    s = _settings()
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=s.presign_ttl_seconds,
    )


def presign_get(bucket: str, key: str) -> str:
    s = _settings()
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=s.presign_ttl_seconds,
    )


def head(bucket: str, key: str) -> dict:
    return _client().head_object(Bucket=bucket, Key=key)


def read_parquet_table(uri_or_bucket: str, key: str | None = None) -> pa.Table:
    """Read a parquet object into an Arrow table.

    Accepts either ``read_parquet_table("s3://bucket/key.parquet")`` or
    ``read_parquet_table(bucket, key)``.
    """
    bucket, obj_key = _split(uri_or_bucket, key)
    body = _client().get_object(Bucket=bucket, Key=obj_key)["Body"].read()
    return pq.read_table(io.BytesIO(body))


def write_parquet_table(table: pa.Table, bucket: str, key: str) -> int:
    """Write an Arrow table as parquet to ``bucket/key``; return byte size."""
    buf = io.BytesIO()
    pq.write_table(table, buf)
    data = buf.getvalue()
    _client().put_object(Bucket=bucket, Key=key, Body=data)
    return len(data)


def write_bytes(bucket: str, key: str, data: bytes, content_type: str) -> int:
    _client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return len(data)


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def _split(uri_or_bucket: str, key: str | None) -> tuple[str, str]:
    if key is not None:
        return uri_or_bucket, key
    if not uri_or_bucket.startswith("s3://"):
        raise ValueError(f"expected an s3:// uri, got {uri_or_bucket!r}")
    rest = uri_or_bucket[len("s3://") :]
    bucket, _, obj_key = rest.partition("/")
    if not obj_key:
        raise ValueError(f"s3 uri is missing a key: {uri_or_bucket!r}")
    return bucket, obj_key

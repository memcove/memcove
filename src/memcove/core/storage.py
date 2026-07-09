"""Object-store (S3/MinIO) helpers for the data plane.

Owns presigned URLs (upload + artifact download), the staging area for
out-of-band uploads, and reading/writing parquet objects with PyArrow.
"""

from __future__ import annotations

import io
from functools import lru_cache

import boto3
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
from botocore.client import Config

from memcove.core.config import Settings, get_settings


@lru_cache
def _client():
    s = get_settings()
    kwargs: dict = dict(
        endpoint_url=s.s3_endpoint,
        region_name=s.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if s.s3_path_style else "auto"},
        ),
    )
    # Pass static keys only when set; otherwise boto3 uses its default credential
    # chain (IRSA / instance profile / env / STS) for a keyless AWS deployment.
    creds = s.static_s3_credentials()
    if creds:
        kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"] = creds
    return boto3.client("s3", **kwargs)


@lru_cache
def _pa_s3fs() -> pafs.S3FileSystem:
    """A PyArrow S3 filesystem mirroring the boto3 client's config.

    Used for streaming writes: ``open_output_stream`` returns a handle that flushes
    to S3 via multipart under the hood, so a large export never sits fully in RAM.
    Static keys only when set; otherwise PyArrow uses the AWS default credential
    chain (IRSA / instance profile / env / STS), same as the boto3 client.
    """
    s = get_settings()
    scheme = "http" if s.s3_endpoint.startswith("http://") else "https"
    endpoint = s.s3_endpoint.split("://", 1)[-1]
    kwargs: dict = dict(
        endpoint_override=endpoint,
        region=s.s3_region,
        scheme=scheme,
        # MinIO / path-style needs virtual addressing OFF; AWS with a real bucket is fine either way.
        force_virtual_addressing=not s.s3_path_style,
    )
    creds = s.static_s3_credentials()
    if creds:
        kwargs["access_key"], kwargs["secret_key"] = creds
    return pafs.S3FileSystem(**kwargs)


def open_output_stream(bucket: str, key: str):
    """Open a streaming write handle to ``bucket/key`` (multipart under the hood).

    Write Arrow batches / bytes to it incrementally instead of building the whole
    object in memory. Caller is responsible for closing it (use a ``with`` block).
    """
    return _pa_s3fs().open_output_stream(f"{bucket}/{key}")


def object_size(uri_or_bucket: str, key: str | None = None) -> int:
    """Content-Length of an S3 object, via a HEAD (no body download).

    Accepts ``object_size("s3://bucket/key")`` or ``object_size(bucket, key)``.
    Used to size-guard ingest before reading the whole object into the pod.
    """
    bucket, obj_key = _split(uri_or_bucket, key)
    return int(head(bucket, obj_key)["ContentLength"])


def _settings() -> Settings:
    return get_settings()


def resolve(location: str, *parts: str) -> tuple[str, str]:
    """Split a bucket-or-bucket/path setting into (bucket, key) and append key parts.

    ``location`` is a ``staging_bucket`` / ``artifacts_bucket`` value: a bare bucket
    (``"my-bucket"``) or a bucket with a sub-path (``"my-bucket/teams/data-eng"``). The
    sub-path becomes a key prefix, so::

        resolve("my-bucket", "uploads", "t_acme", "x.parquet")
            -> ("my-bucket", "uploads/t_acme/x.parquet")
        resolve("my-bucket/teams/data-eng", "uploads", "t_acme", "x.parquet")
            -> ("my-bucket", "teams/data-eng/uploads/t_acme/x.parquet")

    Slashes are normalized so the key never doubles ``/``.
    """
    bucket, _, prefix = location.strip("/").partition("/")
    segments = ([prefix.strip("/")] if prefix else []) + [p.strip("/") for p in parts if p]
    return bucket, "/".join(segments)


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

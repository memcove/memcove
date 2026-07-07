"""Unit tests for bucket-with-sub-path resolution (storage.resolve)."""

from __future__ import annotations

from memcove.core import storage


def test_bare_bucket_writes_at_root():
    assert storage.resolve("my-bucket", "uploads", "t_acme", "x.parquet") == (
        "my-bucket", "uploads/t_acme/x.parquet"
    )


def test_bucket_with_subpath_prefixes_the_key():
    assert storage.resolve("my-bucket/teams/data-eng", "exports", "t_acme", "r.csv") == (
        "my-bucket", "teams/data-eng/exports/t_acme/r.csv"
    )


def test_slashes_normalized_no_double_slash():
    bucket, key = storage.resolve("my-bucket/team/", "uploads", "t_acme", "x.parquet")
    assert bucket == "my-bucket"
    assert "//" not in key
    assert key == "team/uploads/t_acme/x.parquet"


def test_upload_handle_binding_is_prefix_aware():
    # The handle a caller replays must sit under THEIR tenant's uploads path, including
    # any bucket sub-path — this is the cross-tenant guard in ingest._table_from_source.
    _, expected = storage.resolve("my-bucket/team", "uploads", "t_acme")
    mine = storage.resolve("my-bucket/team", "uploads", "t_acme", "f.parquet")[1]
    other = storage.resolve("my-bucket/team", "uploads", "t_evil", "f.parquet")[1]
    root = storage.resolve("my-bucket", "uploads", "t_acme", "f.parquet")[1]  # no sub-path
    assert mine.startswith(expected + "/")
    assert not other.startswith(expected + "/")  # another tenant rejected
    assert not root.startswith(expected + "/")  # a handle minted under a different path rejected

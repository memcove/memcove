"""Unit tests for the s3_parquet ingest allowlist (no infra)."""

from __future__ import annotations

import pytest

from memcove.core.config import get_settings
from memcove.core.errors import IngestError
from memcove.tools.ingest import _check_s3_ingest_allowed, _table_from_source


def test_disabled_when_no_prefixes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_s3_ingest_prefixes", [])
    with pytest.raises(IngestError):
        _check_s3_ingest_allowed("s3://any/x.parquet", s)


def test_allows_uri_within_prefix(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_s3_ingest_prefixes", ["s3://mydata"])
    _check_s3_ingest_allowed("s3://mydata/f.parquet", s)  # must not raise


def test_rejects_sibling_bucket_prefix_confusion(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_s3_ingest_prefixes", ["s3://mydata"])
    # "s3://mydata-evil" must NOT be allowed by the "s3://mydata" prefix.
    with pytest.raises(IngestError):
        _check_s3_ingest_allowed("s3://mydata-evil/f.parquet", s)


def test_upload_handle_rejects_foreign_tenant():
    # A handle for another tenant must be rejected before any storage read.
    src = {"kind": "upload_handle", "handle": "uploads/t_victim/x.parquet"}
    with pytest.raises(IngestError):
        _table_from_source(src, "t_acme")

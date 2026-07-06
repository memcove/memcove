"""S3 credential wiring: static keys when set, default chain (IRSA) when cleared."""

from __future__ import annotations

import pytest

from memcove.core import catalog, storage
from memcove.core.config import Settings, get_settings


def test_static_credentials_present_by_default():
    assert Settings().static_s3_credentials() == ("minio", "minio12345")


@pytest.mark.parametrize(
    "access,secret",
    [("", ""), ("", "x"), ("x", ""), (None, None)],
)
def test_credentials_cleared_defers_to_chain(access, secret):
    assert Settings(s3_access_key=access, s3_secret_key=secret).static_s3_credentials() is None


def _capture(monkeypatch, target_module, func_name):
    calls = {}

    def fake(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(target_module, func_name, fake)
    return calls


def test_catalog_passes_static_keys_when_set(monkeypatch):
    get_settings.cache_clear()
    catalog.get_catalog.cache_clear()
    calls = _capture(monkeypatch, catalog, "load_catalog")
    catalog.get_catalog()
    props = calls["kwargs"]
    assert props["s3.access-key-id"] == "minio"
    assert props["s3.secret-access-key"] == "minio12345"


def test_catalog_omits_keys_when_cleared(monkeypatch):
    monkeypatch.setenv("MEMCOVE_S3_ACCESS_KEY", "")
    monkeypatch.setenv("MEMCOVE_S3_SECRET_KEY", "")
    get_settings.cache_clear()
    catalog.get_catalog.cache_clear()
    calls = _capture(monkeypatch, catalog, "load_catalog")
    catalog.get_catalog()
    props = calls["kwargs"]
    assert "s3.access-key-id" not in props
    assert "s3.secret-access-key" not in props
    get_settings.cache_clear()


def test_storage_client_omits_keys_when_cleared(monkeypatch):
    monkeypatch.setenv("MEMCOVE_S3_ACCESS_KEY", "")
    monkeypatch.setenv("MEMCOVE_S3_SECRET_KEY", "")
    get_settings.cache_clear()
    storage._client.cache_clear()
    calls = _capture(monkeypatch, storage.boto3, "client")
    storage._client()
    kwargs = calls["kwargs"]
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    get_settings.cache_clear()
    storage._client.cache_clear()

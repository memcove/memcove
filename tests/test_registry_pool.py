"""Unit tests for the registry backend singleton + Postgres pool wiring.

Locks: the backend is a lazy singleton keyed on the DSN (rebuilt when the DSN
changes, old one closed), atexit cleanup is registered, close_pool resets; and the
Postgres backend builds psycopg_pool with a borrow-time health check.
"""

from __future__ import annotations

import psycopg_pool

from memcove.core import registry
from memcove.core.registry_backends import PostgresBackend


class _FakeBackend:
    def __init__(self, dsn):
        self.dsn = dsn
        self.closed = False

    def close(self):
        self.closed = True


def test_get_is_lazy_singleton_and_rebuilds_on_dsn_change(monkeypatch):
    built: list = []
    monkeypatch.setattr(
        registry, "make_backend", lambda dsn, **kw: built.append(_FakeBackend(dsn)) or built[-1]
    )
    registered: list = []
    monkeypatch.setattr(registry.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(registry, "_atexit_registered", False)
    registry.close_pool()  # reset state

    monkeypatch.setenv("MEMCOVE_REGISTRY_DSN", "sqlite:///a.db")
    registry.get_settings.cache_clear()
    b1 = registry._get()
    b2 = registry._get()
    assert b1 is b2 and len(built) == 1  # lazy singleton, built once
    assert registry.close_pool in registered  # cleaned up at interpreter exit

    monkeypatch.setenv("MEMCOVE_REGISTRY_DSN", "sqlite:///b.db")
    registry.get_settings.cache_clear()
    b3 = registry._get()
    assert b3 is not b1 and b1.closed and len(built) == 2  # rebuilt, old closed

    registry.close_pool()
    registry.get_settings.cache_clear()


def test_close_pool_closes_and_resets_idempotently(monkeypatch):
    fake = _FakeBackend("x")
    monkeypatch.setattr(registry, "_backend", fake)
    monkeypatch.setattr(registry, "_backend_dsn", "x")

    registry.close_pool()
    assert fake.closed is True and registry._backend is None

    registry.close_pool()  # idempotent
    assert registry._backend is None


def test_postgres_backend_builds_pool_with_health_check(monkeypatch):
    captured: dict = {}

    class _FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, dsn=None, **kwargs):
            captured["dsn"] = dsn
            captured["kwargs"] = kwargs

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)
    PostgresBackend("postgresql://x", min_size=2, max_size=9, timeout=7.0)

    kw = captured["kwargs"]
    assert kw["open"] is True
    assert kw["min_size"] == 2 and kw["max_size"] == 9 and kw["timeout"] == 7.0
    # Health-check on borrow: a stale connection is replaced, not handed out.
    assert kw["check"] is _FakePool.check_connection

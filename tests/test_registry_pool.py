"""Unit tests for the registry connection pool wiring (no Postgres required).

The pool itself is psycopg_pool's; these lock our wiring: lazy singleton with a
borrow-time health check, both connection helpers delegate to it, atexit cleanup,
and close_pool() resets cleanly.
"""

from __future__ import annotations

from memcove.core import registry


class _FakePool:
    """Stand-in for psycopg_pool.ConnectionPool (records construction, no real I/O)."""

    check_connection = staticmethod(lambda conn: None)  # mirror the real API surface

    def __init__(self, dsn=None, **kwargs):
        self.dsn = dsn
        self.kwargs = kwargs
        self.closed = False

    def connection(self):
        return f"conn:{self.dsn}"

    def close(self):
        self.closed = True


def test_get_pool_is_lazy_singleton_with_health_check(monkeypatch):
    built = []

    class _Recording(_FakePool):
        def __init__(self, dsn=None, **kwargs):
            super().__init__(dsn, **kwargs)
            built.append(self)

    registered = []
    monkeypatch.setattr(registry, "ConnectionPool", _Recording)
    monkeypatch.setattr(registry.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(registry, "_pool", None)

    p1 = registry._get_pool()
    p2 = registry._get_pool()
    assert p1 is p2  # reused, not rebuilt
    assert len(built) == 1  # constructed exactly once (lazy singleton)
    assert p1.kwargs["open"] is True
    assert p1.kwargs["min_size"] == registry.get_settings().pg_pool_min_size
    assert p1.kwargs["max_size"] == registry.get_settings().pg_pool_max_size
    # Health-check on borrow: a stale connection (PG restart / idle drop) is replaced,
    # not handed to the caller. Regression guard for the pooling review finding.
    assert p1.kwargs["check"] is _Recording.check_connection
    assert registry.close_pool in registered  # released cleanly at interpreter exit


def test_both_conn_helpers_delegate_to_pool(monkeypatch):
    monkeypatch.setattr(registry, "_get_pool", lambda: _FakePool(dsn="X"))
    # _conn and _conn_tx now share the pool; both hand back its connection() CM.
    assert registry._conn() == "conn:X"
    assert registry._conn_tx() == "conn:X"


def test_close_pool_closes_and_resets_idempotently(monkeypatch):
    fake = _FakePool(dsn="X")
    monkeypatch.setattr(registry, "_pool", fake)

    registry.close_pool()
    assert fake.closed is True
    assert registry._pool is None

    registry.close_pool()  # idempotent: no pool, no error
    assert registry._pool is None

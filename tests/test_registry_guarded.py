"""Unit tests for registry.record_object_guarded (no Postgres required)."""

from __future__ import annotations

import psycopg
import pytest

from memcove.core import registry


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((sql.split()[0], params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log, raise_exc=None):
        self.log = log
        self.raise_exc = raise_exc

    def cursor(self):
        return _FakeCursor(self.log)

    def __enter__(self):
        if self.raise_exc is not None:
            raise self.raise_exc
        return self

    def __exit__(self, *a):
        return False


def test_guarded_write_success_returns_true(monkeypatch):
    log: list = []
    monkeypatch.setattr(registry, "_conn_tx", lambda: _FakeConn(log))
    ok = registry.record_object_guarded(
        "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
        lineage_parents=["src"],
    )
    assert ok is True
    verbs = [v for v, _ in log]
    # object upsert (INSERT) + lineage delete + lineage insert all in one transaction.
    assert "INSERT" in verbs and "DELETE" in verbs


def test_registry_down_returns_false_and_does_not_raise(monkeypatch, caplog):
    # OperationalError = "registry unreachable" -> swallow, data is already committed.
    down = psycopg.OperationalError("connection refused")
    monkeypatch.setattr(registry, "_conn_tx", lambda: _FakeConn([], raise_exc=down))
    ok = registry.record_object_guarded(
        "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
    )
    assert ok is False  # the committed data write must not fail because the registry is down
    assert any("registry drift" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "exc",
    [psycopg.ProgrammingError("syntax error"), TypeError("wrong arg count"), ValueError("bad")],
)
def test_logic_bug_raises_instead_of_silent_pending(monkeypatch, exc):
    # A real bug (bad SQL, wrong param count) must NOT be disguised as pending metadata,
    # or it would silently strip lineage/tags off every write. It must surface loudly.
    monkeypatch.setattr(registry, "_conn_tx", lambda: _FakeConn([], raise_exc=exc))
    with pytest.raises(type(exc)):
        registry.record_object_guarded(
            "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
        )


def test_guarded_write_without_lineage_skips_lineage(monkeypatch):
    log: list = []
    monkeypatch.setattr(registry, "_conn_tx", lambda: _FakeConn(log))
    registry.record_object_guarded(
        "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
    )
    # No lineage_parents -> only the object upsert, no DELETE on the lineage table.
    assert [v for v, _ in log] == ["INSERT"]


@pytest.mark.parametrize("parents", [[], ["a", "b"]])
def test_guarded_write_lineage_present_when_parents_given(monkeypatch, parents):
    log: list = []
    monkeypatch.setattr(registry, "_conn_tx", lambda: _FakeConn(log))
    registry.record_object_guarded(
        "t_acme", "d", table_ident="iceberg.t_acme.d", source="derived",
        lineage_parents=parents,
    )
    # lineage_parents is not None (even empty) -> the DELETE-then-insert lineage reset runs.
    assert "DELETE" in [v for v, _ in log]

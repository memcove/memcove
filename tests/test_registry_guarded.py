"""Unit tests for registry.record_object_guarded (no database required).

Injects a fake backend so the guarded-write contract is exercised in isolation:
success -> True, "registry down" -> False (swallowed), a real bug -> raises.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from memcove.core import registry


class _FakeBackend:
    upsert_object_sql = "INSERT INTO memcove_objects ..."
    insert_lineage_sql = "INSERT INTO memcove_lineage ..."

    def __init__(self, log, *, raise_exc=None, down=False):
        self.log = log
        self.raise_exc = raise_exc
        self._down = down

    @contextmanager
    def connection(self):
        if self.raise_exc is not None:
            raise self.raise_exc
        yield object()

    def execute(self, conn, sql, params=()):
        self.log.append(sql)

    def is_connection_down(self, exc):
        return self._down


def _statements_touching(log, table):
    return [s for s in log if table in s]


def test_guarded_write_success_returns_true(monkeypatch):
    log: list = []
    monkeypatch.setattr(registry, "_get", lambda: _FakeBackend(log))
    ok = registry.record_object_guarded(
        "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
        lineage_parents=["src"],
    )
    assert ok is True
    assert _statements_touching(log, "memcove_objects")       # object upsert ran
    assert _statements_touching(log, "memcove_lineage")       # lineage reset ran


def test_registry_down_returns_false_and_does_not_raise(monkeypatch, caplog):
    down = psycopg.OperationalError("connection refused")
    monkeypatch.setattr(registry, "_get", lambda: _FakeBackend([], raise_exc=down, down=True))
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
    # A real bug must NOT be disguised as pending metadata (down=False -> re-raise).
    monkeypatch.setattr(registry, "_get", lambda: _FakeBackend([], raise_exc=exc, down=False))
    with pytest.raises(type(exc)):
        registry.record_object_guarded(
            "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
        )


def test_guarded_write_without_lineage_skips_lineage(monkeypatch):
    log: list = []
    monkeypatch.setattr(registry, "_get", lambda: _FakeBackend(log))
    registry.record_object_guarded(
        "t_acme", "people", table_ident="iceberg.t_acme.people", source="inline",
    )
    # No lineage_parents -> the object upsert (+ tag reset) runs, but nothing touches lineage.
    assert not _statements_touching(log, "memcove_lineage")


@pytest.mark.parametrize("parents", [[], ["a", "b"]])
def test_guarded_write_lineage_present_when_parents_given(monkeypatch, parents):
    log: list = []
    monkeypatch.setattr(registry, "_get", lambda: _FakeBackend(log))
    registry.record_object_guarded(
        "t_acme", "d", table_ident="iceberg.t_acme.d", source="derived",
        lineage_parents=parents,
    )
    # lineage_parents is not None (even empty) -> the DELETE-then-insert lineage reset runs.
    assert any(s.startswith("DELETE") and "memcove_lineage" in s for s in log)

"""Unit tests for the /health and /ready HTTP probes (no infra needed)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from memcove import server


@pytest.fixture(scope="module")
def client():
    # The streamable-HTTP session manager can only run once per app instance, so
    # share a single client (and lifespan) across the module.
    with TestClient(server.mcp.streamable_http_app()) as c:
        yield c


def test_health_is_always_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.text == "ok"


def test_ready_ok_when_deps_up(client, monkeypatch):
    monkeypatch.setattr(server.registry, "ping", lambda: None)
    monkeypatch.setattr(server, "_trino_reachable", lambda: True)
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"] == {"registry": "ok", "trino": "ok"}


def test_ready_503_when_registry_down(client, monkeypatch):
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(server.registry, "ping", boom)
    monkeypatch.setattr(server, "_trino_reachable", lambda: True)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert "no db" in body["checks"]["registry"]
    assert body["checks"]["trino"] == "ok"


def test_ready_503_when_trino_unreachable(client, monkeypatch):
    monkeypatch.setattr(server.registry, "ping", lambda: None)
    monkeypatch.setattr(server, "_trino_reachable", lambda: False)
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["trino"] == "unreachable"

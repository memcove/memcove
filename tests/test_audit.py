"""Unit tests for the audit log (no infra)."""

from __future__ import annotations

import json
import logging

from memcove.core.audit import audit


def test_audit_emits_structured_json(caplog):
    with caplog.at_level(logging.INFO, logger="memcove.audit"):
        audit("query", tenant="t_acme", sql="SELECT 1", rows=5)
    records = [r for r in caplog.records if r.name == "memcove.audit"]
    assert records
    payload = json.loads(records[-1].message)
    assert payload["event"] == "query"
    assert payload["tenant"] == "t_acme"
    assert payload["rows"] == 5


def test_audit_never_raises_on_odd_fields(caplog):
    class Weird:
        pass

    with caplog.at_level(logging.INFO, logger="memcove.audit"):
        audit("weird", obj=Weird())  # must not raise
    assert any(r.name == "memcove.audit" for r in caplog.records)

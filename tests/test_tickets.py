"""Unit tests for Flight ticket signing (no infra required)."""

from __future__ import annotations

import time

import pytest

from memcove.core.errors import TicketError
from memcove.data_plane import tickets


def test_sign_verify_roundtrip():
    cmd = tickets.read_command("t_acme", "prices")
    assert tickets.verify(tickets.sign(cmd)) == cmd


def test_forged_unsigned_ticket_rejected():
    # A raw (unsigned) command is exactly what an attacker would hand-craft.
    raw = tickets.encode(tickets.read_command("t_acme", "prices"))
    with pytest.raises(TicketError):
        tickets.verify(raw)


def test_tampered_tenant_rejected():
    raw = tickets.sign(tickets.read_command("t_acme", "prices"))
    outer = tickets.decode(raw)
    outer["env"]["cmd"]["tenant"] = "t_intruder"  # swap tenant, keep original sig
    with pytest.raises(TicketError):
        tickets.verify(tickets.encode(outer))


def test_expired_ticket_rejected(monkeypatch):
    raw = tickets.sign(tickets.read_command("t_acme", "prices"))
    real = time.time
    monkeypatch.setattr(tickets.time, "time", lambda: real() + 10_000)
    with pytest.raises(TicketError):
        tickets.verify(raw)


@pytest.mark.parametrize("raw", [b"not json at all", b"{}", b'{"env":{},"sig":123}'])
def test_malformed_ticket_rejected(raw):
    with pytest.raises(TicketError):
        tickets.verify(raw)

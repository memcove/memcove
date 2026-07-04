"""Encode/decode Arrow Flight tickets and descriptors.

A *ticket* (DoGet) or *descriptor command* (DoPut) is a small JSON blob naming
the operation, the tenant, and the target. The control-plane MCP tools mint
these and hand them (base64) to a client, which then streams Arrow batches
in/out of the Flight server out-of-band.

Tickets are HMAC-signed and short-lived: the control plane mints a signed
envelope ``{"env": {"cmd", "exp", "nonce"}, "sig"}`` and the Flight server
rejects any ticket whose signature is invalid or whose ``exp`` has passed. This
closes the forged-ticket hole on the gRPC surface, which the OIDC proxy in front
of the MCP port does not cover.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from memcove.core.config import get_settings
from memcove.core.errors import TicketError


def encode(command: dict[str, Any]) -> bytes:
    """Serialize a command dict to compact JSON bytes."""
    return json.dumps(command, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes) -> dict[str, Any]:
    """Parse ticket/descriptor bytes back into a dict (no verification)."""
    return json.loads(bytes(raw).decode("utf-8"))


def to_b64(command: dict[str, Any]) -> str:
    """Base64 of the raw (unsigned) command — used only in tests/tooling."""
    return base64.b64encode(encode(command)).decode("ascii")


# --- signing ----------------------------------------------------------------

def _canon(obj: dict[str, Any]) -> bytes:
    """Deterministic bytes for signing (sorted keys, compact)."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sig(env: dict[str, Any]) -> str:
    secret = get_settings().flight_ticket_secret.encode("utf-8")
    return base64.b64encode(
        hmac.new(secret, _canon(env), hashlib.sha256).digest()
    ).decode("ascii")


def sign(command: dict[str, Any]) -> bytes:
    """Wrap a command in a signed, expiring envelope (the on-wire ticket)."""
    env = {
        "cmd": command,
        "exp": int(time.time()) + get_settings().flight_ticket_ttl_seconds,
        "nonce": secrets.token_hex(8),
    }
    return encode({"env": env, "sig": _sig(env)})


def to_b64_signed(command: dict[str, Any]) -> str:
    """Base64 of a signed ticket — JSON-safe to return through MCP."""
    return base64.b64encode(sign(command)).decode("ascii")


def verify(raw: bytes) -> dict[str, Any]:
    """Verify a signed ticket and return its command dict, or raise TicketError."""
    try:
        outer = decode(raw)
    except Exception as exc:  # noqa: BLE001 - malformed bytes
        raise TicketError("malformed ticket") from exc
    env = outer.get("env") if isinstance(outer, dict) else None
    sig = outer.get("sig") if isinstance(outer, dict) else None
    if not isinstance(env, dict) or not isinstance(sig, str):
        raise TicketError("malformed ticket envelope")
    if not hmac.compare_digest(sig, _sig(env)):
        raise TicketError("invalid ticket signature")
    if int(env.get("exp", 0)) < int(time.time()):
        raise TicketError("ticket expired")
    cmd = env.get("cmd")
    if not isinstance(cmd, dict):
        raise TicketError("malformed ticket command")
    return cmd


# --- command builders -------------------------------------------------------

def read_command(tenant: str, name: str) -> dict[str, Any]:
    """DoGet: stream a whole dataset back as Arrow batches."""
    return {"op": "read", "tenant": tenant, "name": name}


def query_command(tenant: str, sql: str) -> dict[str, Any]:
    """DoGet: stream the result of a guarded SELECT back as Arrow batches."""
    return {"op": "query", "tenant": tenant, "sql": sql}


def ingest_command(tenant: str, name: str, mode: str) -> dict[str, Any]:
    """DoPut: stream Arrow batches into a dataset."""
    return {"op": "ingest", "tenant": tenant, "name": name, "mode": mode}

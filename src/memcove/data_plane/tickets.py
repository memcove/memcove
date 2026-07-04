"""Encode/decode Arrow Flight tickets and descriptors.

A *ticket* (DoGet) or *descriptor command* (DoPut) is a small JSON blob naming
the operation, the tenant, and the target. The control-plane MCP tools mint
these and hand them (base64) to a client, which then streams Arrow batches
in/out of the Flight server out-of-band.

Note: tickets are not signed yet (auth deferred — see core/tenancy.py). They
carry the tenant the same way the MCP header does today.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def encode(command: dict[str, Any]) -> bytes:
    """Serialize a command dict to compact JSON bytes (the on-wire ticket)."""
    return json.dumps(command, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes) -> dict[str, Any]:
    """Parse Flight ticket/descriptor command bytes back into a dict."""
    return json.loads(bytes(raw).decode("utf-8"))


def to_b64(command: dict[str, Any]) -> str:
    """Base64 of the encoded command — JSON-safe to return through MCP."""
    return base64.b64encode(encode(command)).decode("ascii")


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

"""Decode inline payloads into Arrow tables for the ingest write path."""

from __future__ import annotations

import base64
import io

import pyarrow as pa
import pyarrow.ipc as ipc

from memcove.core.errors import IngestError


def from_json_records(records: list[dict]) -> pa.Table:
    """Build an Arrow table from a list of JSON row objects."""
    if not records:
        raise IngestError("inline json_records payload is empty")
    try:
        return pa.Table.from_pylist(records)
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"could not build table from json records: {exc}") from exc


def from_arrow_ipc_b64(data_b64: str) -> pa.Table:
    """Decode a base64 Arrow IPC stream into a table."""
    try:
        raw = base64.b64decode(data_b64)
        with ipc.open_stream(io.BytesIO(raw)) as reader:
            return reader.read_all()
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"could not decode arrow_ipc payload: {exc}") from exc


def estimate_bytes(table: pa.Table) -> int:
    return table.nbytes

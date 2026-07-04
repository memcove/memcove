"""Catalog operations: describe / get / list / drop, plus shared helpers."""

from __future__ import annotations

from memcove.core import catalog, registry, trino_client
from memcove.core.config import get_settings
from memcove.core.errors import ObjectNotFoundError
from memcove.core.models import (
    ColumnSchema,
    Lineage,
    MemoryObject,
    PreviewResult,
    SourceKind,
)
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select, wrap_preview


def _qualified(tenant: str, label: str) -> str:
    cat = get_settings().trino_catalog
    return f'"{cat}"."{tenant}"."{label}"'


def _row_count(tenant: str, label: str) -> int | None:
    try:
        return int(trino_client.scalar(f"SELECT count(*) FROM {_qualified(tenant, label)}"))
    except Exception:  # noqa: BLE001 - count is best-effort metadata
        return None


def describe_object(tenant: str, label: str) -> MemoryObject:
    """Full metadata for an object: schema, source, lineage, row count."""
    label = validate_label(label)
    reg = registry.get_object(tenant, label)
    if reg is None and not catalog.table_exists(tenant, label):
        raise ObjectNotFoundError(f"object '{label}' does not exist")

    schema = [ColumnSchema(name=n, type=t) for n, t in catalog.load_schema(tenant, label)]
    parents = registry.get_parents(tenant, label)

    source = SourceKind(reg["source"]) if reg else SourceKind.S3_PARQUET
    return MemoryObject(
        tenant=tenant,
        label=label,
        table_ident=f"{get_settings().trino_catalog}.{tenant}.{label}",
        source=source,
        source_ref=reg.get("source_ref") if reg else None,
        schema=schema,
        row_count=_row_count(tenant, label),
        tags=list(reg["tags"]) if reg else [],
        lineage=Lineage(
            parents=parents,
            producing_sql=reg.get("producing_sql") if reg else None,
        ),
        created_at=reg.get("created_at") if reg else None,
        updated_at=reg.get("updated_at") if reg else None,
    )


def list_objects(tenant: str, tags: list[str] | None = None) -> list[dict]:
    """Lightweight object summaries (no per-object row count)."""
    rows = registry.list_objects(tenant, tags)
    return [
        {
            "label": r["label"],
            "source": r["source"],
            "tags": list(r["tags"]),
            "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
        }
        for r in rows
    ]


def get_object(
    tenant: str, label: str, mode: str = "preview", limit: int | None = None
) -> dict:
    """Read an object as preview rows, schema, or stats."""
    label = validate_label(label)
    if not catalog.table_exists(tenant, label):
        raise ObjectNotFoundError(f"object '{label}' does not exist")

    if mode == "schema":
        return {"schema": [{"name": n, "type": t} for n, t in catalog.load_schema(tenant, label)]}

    if mode == "stats":
        return {
            "row_count": _row_count(tenant, label),
            "schema": [{"name": n, "type": t} for n, t in catalog.load_schema(tenant, label)],
        }

    if mode == "preview":
        settings = get_settings()
        cap = min(limit or settings.preview_row_cap, settings.preview_row_cap)
        guard = validate_select(f"SELECT * FROM {label}", tenant, settings.trino_catalog)
        columns, rows = trino_client.execute(wrap_preview(guard.sql, cap))
        truncated = len(rows) > cap
        result = PreviewResult(
            columns=columns,
            rows=rows[:cap],
            row_count=min(len(rows), cap),
            truncated=truncated,
        )
        return result.model_dump()

    raise ValueError(f"unknown mode {mode!r}; expected preview|schema|stats")


def drop_object(tenant: str, label: str) -> dict:
    label = validate_label(label)
    catalog.drop_table(tenant, label)
    registry.delete_object(tenant, label)
    return {"dropped": label}

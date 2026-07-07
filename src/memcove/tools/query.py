"""Query tool — guarded read-only SQL over a tenant's objects."""

from __future__ import annotations

from memcove.core import scratch, trino_client
from memcove.core.audit import audit
from memcove.core.config import get_settings
from memcove.core.models import PreviewResult
from memcove.core.sql_guard import validate_select, wrap_preview


def run_query(tenant: str, sql: str, limit: int | None = None) -> PreviewResult:
    """Validate, qualify, and run a read-only SELECT; return capped rows.

    The SELECT may reference lakehouse objects, the shared reference plane, and — via the
    ``scratch.<label>`` alias — the caller's scratchpad, joined together in one query.
    """
    settings = get_settings()
    cap = min(limit or settings.preview_row_cap, settings.preview_row_cap)
    scratch_cat, scratch_schema = scratch.guard_params(tenant)
    guard = validate_select(
        sql, tenant_ns=tenant, catalog=settings.trino_catalog,
        shared_schemas=settings.shared_schemas,
        scratch_catalog=scratch_cat, scratch_schema=scratch_schema,
    )
    columns, rows = trino_client.execute(wrap_preview(guard.sql, cap), run_as=tenant)
    truncated = len(rows) > cap
    audit("query", tenant=tenant, sql=guard.sql, rows=min(len(rows), cap), truncated=truncated)
    return PreviewResult(
        columns=columns,
        rows=rows[:cap],
        row_count=min(len(rows), cap),
        truncated=truncated,
    )

"""Query tool — guarded read-only SQL over a tenant's objects."""

from __future__ import annotations

from memcove.core import trino_client
from memcove.core.config import get_settings
from memcove.core.models import PreviewResult
from memcove.core.sql_guard import validate_select, wrap_preview


def run_query(tenant: str, sql: str, limit: int | None = None) -> PreviewResult:
    """Validate, qualify, and run a read-only SELECT; return capped rows."""
    settings = get_settings()
    cap = min(limit or settings.preview_row_cap, settings.preview_row_cap)
    guard = validate_select(sql, tenant_ns=tenant, catalog=settings.trino_catalog)
    columns, rows = trino_client.execute(wrap_preview(guard.sql, cap))
    truncated = len(rows) > cap
    return PreviewResult(
        columns=columns,
        rows=rows[:cap],
        row_count=min(len(rows), cap),
        truncated=truncated,
    )

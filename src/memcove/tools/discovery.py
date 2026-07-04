"""Reference-plane discovery — a curated alternative to information_schema.

The SQL guard denies agents ``information_schema``/``system``, so they cannot
enumerate the shared reference plane themselves. This tool lists the configured
shared schemas and their tables/columns using Memcove's own (service) Trino
identity, giving agents a safe, scoped way to see what reference data exists.
"""

from __future__ import annotations

from memcove.core import trino_client
from memcove.core.config import get_settings


def discover_reference_data() -> dict:
    """List tables + columns of every configured shared reference schema."""
    settings = get_settings()
    cat = settings.trino_catalog
    schemas = []
    for sch in (s for s in settings.shared_schemas if s):
        sql = (
            f'SELECT table_name, column_name, data_type '
            f'FROM "{cat}".information_schema.columns '
            f"WHERE table_schema = '{sch}' "
            f'ORDER BY table_name, ordinal_position'
        )
        try:
            _, rows = trino_client.execute(sql)  # service principal, not the tenant
        except Exception:  # noqa: BLE001 - schema may not exist yet; report it empty
            rows = []
        tables: dict[str, list[dict]] = {}
        for table_name, column_name, data_type in rows:
            tables.setdefault(table_name, []).append(
                {"name": column_name, "type": data_type}
            )
        schemas.append(
            {
                "schema": sch,
                "tables": [{"name": t, "columns": c} for t, c in tables.items()],
            }
        )
    return {"schemas": schemas}

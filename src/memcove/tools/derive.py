"""Derive tool — materialize a new object from a guarded SELECT (CTAS)."""

from __future__ import annotations

from memcove.core import catalog, registry, trino_client
from memcove.core.config import get_settings
from memcove.core.errors import ObjectExistsError, ObjectNotFoundError
from memcove.core.models import MemoryObject, SourceKind
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select
from memcove.tools.objects import describe_object


def derive_object(
    tenant: str,
    new_label: str,
    sql: str,
    mode: str = "create",
    tags: list[str] | None = None,
) -> MemoryObject:
    """Run ``CREATE TABLE <tenant>.<new_label> AS <validated select>`` and track lineage."""
    new_label = validate_label(new_label)
    settings = get_settings()
    guard = validate_select(sql, tenant_ns=tenant, catalog=settings.trino_catalog)

    exists = catalog.table_exists(tenant, new_label)
    if mode == "create" and exists:
        raise ObjectExistsError(
            f"object '{new_label}' already exists (use mode=replace)"
        )
    if mode == "replace" and exists:
        catalog.drop_table(tenant, new_label)
    if mode not in ("create", "replace"):
        raise ValueError(f"unknown mode {mode!r}; expected create|replace")

    trino_client.ensure_schema(tenant)
    target = f'"{settings.trino_catalog}"."{tenant}"."{new_label}"'
    trino_client.execute_update(f"CREATE TABLE {target} AS {guard.sql}")

    # Only labels that actually exist as objects count as lineage parents.
    parents = [lbl for lbl in guard.referenced_labels if catalog.table_exists(tenant, lbl)]
    registry.record_object(
        tenant,
        new_label,
        table_ident=f"{settings.trino_catalog}.{tenant}.{new_label}",
        source=SourceKind.DERIVED.value,
        producing_sql=guard.sql,
        tags=tags or [],
    )
    registry.set_lineage(tenant, new_label, parents)
    return describe_object(tenant, new_label)

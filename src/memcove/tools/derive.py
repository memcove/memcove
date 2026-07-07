"""Derive tool — materialize a new object from a guarded SELECT (CTAS)."""

from __future__ import annotations

from memcove.core import catalog, registry, scratch, trino_client
from memcove.core.audit import audit
from memcove.core.config import get_settings
from memcove.core.errors import ObjectExistsError, SchemaMismatchError
from memcove.core.models import MemoryObject, SourceKind
from memcove.core.naming import validate_label
from memcove.core.sql_guard import validate_select
from memcove.tools.objects import describe_object, pending_object


def _assert_replace_keeps_shape(tenant: str, new_label: str, select_sql: str) -> None:
    """Reject a derive replace that changes the object's columns.

    Unifies the replace contract with the ingest path: ``replace`` never reshapes an
    object. derive's schema follows the SELECT, so we compare the existing table's
    column names to the query's output columns (via a zero-row probe). Type-only
    changes on identical column names follow the SELECT; column add/remove/rename is
    rejected. To change shape, ``forget()`` then ``create()``.
    """
    existing_cols = {n for n, _ in catalog.load_schema(tenant, new_label)}
    new_cols, _ = trino_client.execute(
        f"SELECT * FROM ({select_sql}) _probe WHERE 1 = 0", run_as=tenant
    )
    new_set = set(new_cols)
    if existing_cols != new_set:
        added = sorted(new_set - existing_cols)
        removed = sorted(existing_cols - new_set)
        raise SchemaMismatchError(
            f"derive replace of '{new_label}' changes columns "
            f"(added={added}; removed={removed}); forget() then create() to change shape"
        )


def _derive_to_scratch(tenant: str, new_label: str, guard, mode: str) -> MemoryObject:
    """CTAS into the DuckDB scratchpad. Ephemeral, not tracked in the durable registry."""
    exists = scratch.table_exists(tenant, new_label)
    if mode == "create" and exists:
        raise ObjectExistsError(
            f"scratch object '{new_label}' already exists (use mode=replace)"
        )
    scratch.create_as_select(tenant, new_label, guard.sql, replace=(mode == "replace"))
    audit("derive", tenant=tenant, target=new_label, mode=mode, sql=guard.sql, store="scratch")
    return scratch.describe(tenant, new_label)


def derive_object(
    tenant: str,
    new_label: str,
    sql: str,
    mode: str = "create",
    tags: list[str] | None = None,
    target: str = "lakehouse",
) -> MemoryObject:
    """Materialize ``<tenant>.<new_label>`` from a validated SELECT (CTAS).

    ``create`` uses ``CREATE TABLE`` (exclusive — fails if it already exists).
    ``replace`` uses ``CREATE OR REPLACE TABLE`` (Iceberg connector, Trino server
    431+): an atomic swap with no drop window, so a concurrent reader always sees a
    valid table version.

    ``target='scratch'`` materializes into the ephemeral DuckDB scratchpad instead of the
    durable lakehouse. The SELECT can read lakehouse + reference + scratch, so a scratch
    dataset is a fast, joinable derivation of big tables.
    """
    new_label = validate_label(new_label)
    if mode not in ("create", "replace"):
        raise ValueError(f"unknown mode {mode!r}; expected create|replace")
    if target not in ("lakehouse", "scratch"):
        raise ValueError(f"unknown target {target!r}; expected lakehouse|scratch")
    settings = get_settings()
    scratch_cat, scratch_schema = scratch.guard_params(tenant)
    guard = validate_select(
        sql, tenant_ns=tenant, catalog=settings.trino_catalog,
        shared_schemas=settings.shared_schemas,
        scratch_catalog=scratch_cat, scratch_schema=scratch_schema,
    )

    if target == "scratch":
        return _derive_to_scratch(tenant, new_label, guard, mode)

    exists = catalog.table_exists(tenant, new_label)
    if mode == "create" and exists:
        raise ObjectExistsError(f"object '{new_label}' already exists (use mode=replace)")
    if mode == "replace" and exists:
        _assert_replace_keeps_shape(tenant, new_label, guard.sql)

    trino_client.ensure_schema(tenant)
    target = f'"{settings.trino_catalog}"."{tenant}"."{new_label}"'
    # replace is an atomic swap; create is exclusive. Using plain CREATE TABLE for
    # create means Trino itself rejects a concurrent create that raced past the
    # exists-check above, instead of one derive silently clobbering the other.
    verb = "CREATE OR REPLACE TABLE" if mode == "replace" else "CREATE TABLE"
    trino_client.execute_update(f"{verb} {target} AS {guard.sql}", run_as=tenant)
    audit("derive", tenant=tenant, target=new_label, mode=mode, sql=guard.sql)

    # Only labels that actually exist as objects count as lineage parents.
    parents = [lbl for lbl in guard.referenced_labels if catalog.table_exists(tenant, lbl)]
    ok = registry.record_object_guarded(
        tenant,
        new_label,
        table_ident=f"{settings.trino_catalog}.{tenant}.{new_label}",
        source=SourceKind.DERIVED.value,
        producing_sql=guard.sql,
        tags=tags or [],
        lineage_parents=parents,  # object row + lineage commit in one transaction
    )
    if not ok:
        return pending_object(
            tenant, new_label, source=SourceKind.DERIVED,
            producing_sql=guard.sql, parents=parents, tags=tags or [],
        )
    return describe_object(tenant, new_label)

"""PyIceberg REST-catalog client — the write / ingest path.

Ingested data (inline, s3 parquet, or uploaded parquet) becomes a PyArrow
table and is written here. Reads and derivations go through Trino instead
(see ``trino_client.py``); both speak to the same REST catalog + MinIO.
"""

from __future__ import annotations

from functools import lru_cache

import pyarrow as pa
from pyiceberg.catalog import Catalog, load_catalog

from memcove.core.config import get_settings
from memcove.core.errors import ObjectExistsError, ObjectNotFoundError

WriteMode = str  # "create" | "replace" | "append"


@lru_cache
def get_catalog() -> Catalog:
    s = get_settings()
    return load_catalog(
        s.iceberg_catalog_name,
        **{
            "type": "rest",
            "uri": s.iceberg_rest_uri,
            "warehouse": s.iceberg_warehouse,
            "s3.endpoint": s.s3_endpoint,
            "s3.access-key-id": s.s3_access_key,
            "s3.secret-access-key": s.s3_secret_key,
            "s3.region": s.s3_region,
            "s3.path-style-access": str(s.s3_path_style).lower(),
        },
    )


def ensure_namespace(namespace: str) -> None:
    get_catalog().create_namespace_if_not_exists((namespace,))


def table_exists(namespace: str, label: str) -> bool:
    return get_catalog().table_exists(f"{namespace}.{label}")


def list_labels(namespace: str) -> list[str]:
    try:
        idents = get_catalog().list_tables((namespace,))
    except Exception:  # noqa: BLE001 - namespace may not exist yet
        return []
    return [ident[-1] for ident in idents]


def drop_table(namespace: str, label: str) -> None:
    cat = get_catalog()
    ident = f"{namespace}.{label}"
    if not cat.table_exists(ident):
        raise ObjectNotFoundError(f"object '{label}' does not exist")
    cat.drop_table(ident)


def write_arrow(
    namespace: str, label: str, table: pa.Table, mode: WriteMode = "create"
) -> int:
    """Create/replace/append an Iceberg table from an Arrow table.

    Returns the number of rows written in this call.
    """
    cat = get_catalog()
    ensure_namespace(namespace)
    ident = f"{namespace}.{label}"
    exists = cat.table_exists(ident)

    if mode == "create":
        if exists:
            raise ObjectExistsError(
                f"object '{label}' already exists (use mode=replace or mode=append)"
            )
        iceberg_table = cat.create_table(ident, schema=table.schema)
        iceberg_table.append(table)
    elif mode == "replace":
        if exists:
            cat.drop_table(ident)
        iceberg_table = cat.create_table(ident, schema=table.schema)
        iceberg_table.append(table)
    elif mode == "append":
        iceberg_table = (
            cat.load_table(ident) if exists else cat.create_table(ident, schema=table.schema)
        )
        iceberg_table.append(table)
    else:
        raise ValueError(f"unknown write mode: {mode!r}")

    return table.num_rows


def load_schema(namespace: str, label: str) -> list[tuple[str, str]]:
    """Return [(column_name, iceberg_type), ...] for an object."""
    cat = get_catalog()
    ident = f"{namespace}.{label}"
    if not cat.table_exists(ident):
        raise ObjectNotFoundError(f"object '{label}' does not exist")
    tbl = cat.load_table(ident)
    return [(f.name, str(f.field_type)) for f in tbl.schema().fields]

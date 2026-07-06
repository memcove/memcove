"""PyIceberg REST-catalog client — the write / ingest path.

Ingested data (inline, s3 parquet, or uploaded parquet) becomes a PyArrow
table and is written here. Reads and derivations go through Trino instead
(see ``trino_client.py``); both speak to the same REST catalog + MinIO.
"""

from __future__ import annotations

import re
from functools import lru_cache

import pyarrow as pa
from pyiceberg.catalog import Catalog, load_catalog

from memcove.core.config import get_settings
from memcove.core.errors import ObjectExistsError, ObjectNotFoundError, SchemaMismatchError

WriteMode = str  # "create" | "replace" | "append"


def _norm_type(t: pa.DataType) -> str:
    """Canonical type string for schema comparison.

    Iceberg forces several arrow types onto a canonical storage form, and
    ``Schema.as_arrow()`` reads them back in that form. Comparing the raw
    ``str(type)`` of an incoming write against the read-back schema would then
    false-reject a genuinely same-shape replace/append. Fold every conversion
    Iceberg makes so both sides converge:

      * ``large_string`` / ``large_binary`` / ``large_list`` <- string/binary/list
        (an arrow encoding detail Iceberg picks on read-back).
      * ``timestamp[ns|ms|s]`` -> ``timestamp[us]`` (Iceberg timestamps are microsecond).
      * ``int8`` / ``int16`` -> ``int32`` (Iceberg has no integer narrower than 32-bit).

    Applied to both sides, so a real retype (e.g. ``int64`` -> ``string``) still fails.
    Regex-based so the folding reaches into nested ``list``/``struct`` types too.
    """
    s = str(t).replace("large_", "")
    s = re.sub(r"timestamp\[(ns|ms|s)", "timestamp[us", s)
    s = re.sub(r"\bint(8|16)\b", "int32", s)
    return s


def _assert_schema_compatible(
    existing: pa.Schema, incoming: pa.Schema, *, op: str, label: str
) -> None:
    """Reject a replace/append whose schema differs from the existing object.

    Compatibility is deterministic: the set of column names must match and each
    column's Arrow type must be equal (nullability and large-vs-not encoding are
    ignored — Iceberg matches by name). A changed shape is rejected rather than
    silently evolved, so downstream SQL never breaks. To change shape, ``forget()``
    then ``create()``.
    """
    existing_map = {f.name: _norm_type(f.type) for f in existing}
    incoming_map = {f.name: _norm_type(f.type) for f in incoming}
    if existing_map == incoming_map:
        return
    added = sorted(set(incoming_map) - set(existing_map))
    removed = sorted(set(existing_map) - set(incoming_map))
    retyped = sorted(
        f"{n} ({existing_map[n]} -> {incoming_map[n]})"
        for n in set(existing_map) & set(incoming_map)
        if existing_map[n] != incoming_map[n]
    )
    parts = []
    if added:
        parts.append(f"added={added}")
    if removed:
        parts.append(f"removed={removed}")
    if retyped:
        parts.append(f"retyped={retyped}")
    raise SchemaMismatchError(
        f"{op} of '{label}' is schema-incompatible with the existing object "
        f"({'; '.join(parts)}); forget() then create() to change an object's shape"
    )


@lru_cache
def get_catalog() -> Catalog:
    s = get_settings()
    props = {
        "type": "rest",
        "uri": s.iceberg_rest_uri,
        "warehouse": s.iceberg_warehouse,
        "s3.endpoint": s.s3_endpoint,
        "s3.region": s.s3_region,
        "s3.path-style-access": str(s.s3_path_style).lower(),
    }
    # Static keys only when set; otherwise PyIceberg's S3FileIO uses the AWS
    # default credential chain (IRSA / instance profile / env / STS).
    creds = s.static_s3_credentials()
    if creds:
        props["s3.access-key-id"], props["s3.secret-access-key"] = creds
    return load_catalog(s.iceberg_catalog_name, **props)


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


def list_namespaces() -> list[str]:
    """All tenant namespaces known to the catalog (single-component names)."""
    return [ns[0] for ns in get_catalog().list_namespaces() if ns]


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
            # Atomic full-table replace: overwrite() commits delete+append in a
            # single catalog transaction, so a concurrent reader sees the old rows
            # or the new rows, never a missing table (the old drop-then-create had a
            # window where a crash lost the data and a reader saw the table vanish).
            iceberg_table = cat.load_table(ident)
            _assert_schema_compatible(
                iceberg_table.schema().as_arrow(), table.schema, op="replace", label=label
            )
            iceberg_table.overwrite(table)
        else:
            iceberg_table = cat.create_table(ident, schema=table.schema)
            iceberg_table.append(table)
    elif mode == "append":
        if exists:
            iceberg_table = cat.load_table(ident)
            _assert_schema_compatible(
                iceberg_table.schema().as_arrow(), table.schema, op="append", label=label
            )
            iceberg_table.append(table)
        else:
            iceberg_table = cat.create_table(ident, schema=table.schema)
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

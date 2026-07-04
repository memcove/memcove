"""SQL safety gateway.

Makes the "raw SQL escape hatch" safe under multi-tenant, private-per-tenant
isolation. Every statement is parsed with sqlglot and must satisfy:

  * exactly one statement,
  * read-only (only SELECT / set-operations / CTEs; no DDL or DML),
  * every physical table reference resolves inside the caller's namespace
    (bare ``label`` is qualified to ``<catalog>.<tenant_ns>.<label>``;
    references to any other catalog or namespace are rejected).

Materialization (CTAS) is never expressed in user SQL — callers go through
``derive_object``, which wraps the *validated* SELECT itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from memcove.core.errors import SqlGuardError

_DIALECT = "trino"

# Any of these appearing anywhere in the tree means the statement is not a pure read.
_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,  # catch-all for unparsed statements like SET/CALL/GRANT
)

_ALLOWED_TOP = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

# Metadata / enumeration schemas: denied even inside the allowed catalog, because they
# let a caller enumerate other tenants' schemas under an otherwise read-only rule.
_DENY_SCHEMAS = frozenset(
    {"information_schema", "system", "jdbc", "metadata", "sys", "pg_catalog"}
)


@dataclass
class GuardedQuery:
    sql: str  # validated + tenant-qualified SQL (Trino dialect)
    referenced_labels: list[str] = field(default_factory=list)


def _cte_names(tree: exp.Expression) -> set[str]:
    names: set[str] = set()
    for with_ in tree.find_all(exp.With):
        for cte in with_.expressions:
            alias = cte.alias
            if alias:
                names.add(alias.lower())
    return names


def validate_select(
    sql: str, tenant_ns: str, catalog: str, shared_schemas: list[str] | None = None
) -> GuardedQuery:
    """Validate a read-only statement and resolve every table reference.

    Bare names and references to the caller's own schema are qualified to ``tenant_ns``;
    references to a schema in ``shared_schemas`` (the read-only reference plane) resolve
    to themselves; every other namespace, foreign catalog, or metadata schema (e.g.
    ``information_schema``, ``system``) is rejected. Comparison is case-folded, so a
    quoted mixed-case identifier cannot smuggle in a foreign schema.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise SqlGuardError("empty SQL")

    try:
        statements = sqlglot.parse(stripped, read=_DIALECT)
    except Exception as exc:  # noqa: BLE001 - surface parse failures cleanly
        raise SqlGuardError(f"could not parse SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlGuardError("exactly one statement is allowed")

    tree = statements[0]
    if not isinstance(tree, _ALLOWED_TOP):
        raise SqlGuardError(
            f"only read-only SELECT statements are allowed, got {type(tree).__name__}"
        )
    for forbidden in _FORBIDDEN:
        node = tree.find(forbidden)
        if node is not None:
            raise SqlGuardError(
                f"statement contains a non-read operation ({type(node).__name__})"
            )

    cte_names = _cte_names(tree)
    shared = {s.strip().lower() for s in (shared_schemas or []) if s.strip()}
    tenant_lc = tenant_ns.lower()
    cat_lc = catalog.lower()
    referenced: list[str] = []

    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        db = (table.text("db") or "").lower()
        cat = (table.text("catalog") or "").lower()

        # A CTE referenced by bare name is not a physical table.
        if not db and not cat and name in cte_names:
            continue

        # Fail closed on any FROM-item we can't classify as a plain named table
        # (e.g. a polymorphic TABLE(system.query(...)) function parses as an
        # empty-name exp.Table). If we can't prove it stays in-tenant, reject it.
        if not name:
            raise SqlGuardError("unsupported table expression in FROM clause")

        # Foreign catalogs (system, jdbc, other Trino catalogs) are never permitted.
        if cat and cat != cat_lc:
            raise SqlGuardError(
                f"cross-catalog reference '{cat}.{db}.{name}' is not permitted"
            )
        # Metadata/enumeration schemas are denied even inside the allowed catalog.
        if db in _DENY_SCHEMAS:
            raise SqlGuardError(
                f"reference to restricted schema '{db}' is not permitted"
            )

        if not db or db == tenant_lc:
            # Bare name or the caller's own schema -> the caller's private namespace.
            table.set("db", exp.to_identifier(tenant_ns))
        elif db in shared:
            # Shared read-only reference plane: resolves to itself (canonical lowercase).
            table.set("db", exp.to_identifier(db))
        else:
            raise SqlGuardError(
                f"cross-namespace reference '{db}.{name}' is not permitted; you may "
                f"only reference your own objects or the shared reference plane"
            )
        table.set("catalog", exp.to_identifier(catalog))
        if name and name not in referenced:
            referenced.append(name)

    return GuardedQuery(sql=tree.sql(dialect=_DIALECT), referenced_labels=referenced)


def wrap_preview(validated_sql: str, row_cap: int) -> str:
    """Cap a validated SELECT at ``row_cap`` rows, preserving any ORDER BY.

    The limit is applied to the query itself (not via an outer ``SELECT * FROM
    (...)`` wrapper, which would discard the inner ordering in Trino). One extra
    row over the cap is requested so callers can detect truncation.
    """
    inner = validated_sql.strip().rstrip(";")
    try:
        tree = sqlglot.parse_one(inner, read=_DIALECT)
    except Exception:  # noqa: BLE001 - fall back to a safe (unordered) wrapper
        return f"SELECT * FROM (\n{inner}\n) AS _memcove_q LIMIT {row_cap + 1}"

    existing = tree.args.get("limit")
    if existing is None:
        tree = tree.limit(row_cap + 1)  # +1 → caller can tell if more rows exist
    else:
        try:
            current = int(existing.expression.name)
            if current > row_cap:
                tree = tree.limit(row_cap)
        except (AttributeError, ValueError):
            tree = tree.limit(row_cap)
    return tree.sql(dialect=_DIALECT)

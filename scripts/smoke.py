"""End-to-end smoke test for Memcove — drives the full agent journey in-process.

Requires the docker-compose stack to be up (Trino, MinIO, Iceberg REST, Postgres):

    docker compose up -d
    # wait ~20s for Trino to be ready
    python scripts/smoke.py

Flow: ingest inline -> get/query -> ingest second object -> derive (join) ->
describe (lineage) -> presigned upload round-trip -> export artifact + fetch.
"""

from __future__ import annotations

import io
import sys

import requests

from memcove.core import registry
from memcove.core.tenancy import normalize_tenant
from memcove.tools import artifacts, derive, ingest, objects, query

TENANT = normalize_tenant("smoke")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    registry.init_db()

    section("ingest inline: customers")
    ingest.ingest_object(
        TENANT,
        "customers",
        {
            "kind": "inline",
            "format": "json_records",
            "records": [
                {"id": 1, "name": "Ada", "region": "EU"},
                {"id": 2, "name": "Grace", "region": "US"},
                {"id": 3, "name": "Linus", "region": "EU"},
            ],
        },
        mode="replace",
    )
    print(objects.get_object(TENANT, "customers", mode="schema"))

    section("ingest inline: orders")
    ingest.ingest_object(
        TENANT,
        "orders",
        {
            "kind": "inline",
            "format": "json_records",
            "records": [
                {"order_id": 10, "customer_id": 1, "amount": 99.0},
                {"order_id": 11, "customer_id": 1, "amount": 12.5},
                {"order_id": 12, "customer_id": 2, "amount": 40.0},
            ],
        },
        mode="replace",
    )

    section("query: count customers")
    res = query.run_query(TENANT, "SELECT region, count(*) AS n FROM customers GROUP BY region")
    print(res.columns, res.rows)

    section("derive: revenue_by_customer (join)")
    derive.derive_object(
        TENANT,
        "revenue_by_customer",
        """
        SELECT c.id, c.name, sum(o.amount) AS revenue
        FROM customers c JOIN orders o ON o.customer_id = c.id
        GROUP BY c.id, c.name
        """,
        mode="replace",
    )
    meta = objects.describe_object(TENANT, "revenue_by_customer")
    print("lineage parents:", meta.lineage.parents)
    assert set(meta.lineage.parents) == {"customers", "orders"}, meta.lineage.parents

    section("presigned upload round-trip")
    import pyarrow as pa
    import pyarrow.parquet as pq

    ticket = ingest.request_upload(TENANT, "uploaded")
    buf = io.BytesIO()
    pq.write_table(pa.table({"k": [1, 2], "v": ["a", "b"]}), buf)
    put = requests.put(
        ticket.presigned_url,
        data=buf.getvalue(),
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
    )
    put.raise_for_status()
    ingest.ingest_object(
        TENANT, "uploaded", {"kind": "upload_handle", "handle": ticket.upload_handle}, mode="replace"
    )
    print(objects.get_object(TENANT, "uploaded", mode="preview"))

    section("export artifact (parquet) + fetch")
    art = artifacts.export_artifact(TENANT, fmt="parquet", label="revenue_by_customer")
    print("artifact:", art.uri, art.row_count, "rows")
    got = requests.get(art.presigned_url, timeout=30)
    got.raise_for_status()
    table = pq.read_table(io.BytesIO(got.content))
    assert table.num_rows == art.row_count
    print("fetched parquet columns:", table.column_names)

    section("tenant isolation check")
    other = normalize_tenant("intruder")
    try:
        query.run_query(other, f'SELECT * FROM "{TENANT}".customers')
        print("ISOLATION FAILURE: cross-namespace query was allowed")
        return 1
    except Exception as exc:  # noqa: BLE001
        print("blocked as expected:", type(exc).__name__)

    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

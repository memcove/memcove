"""Guided E2E demo: an LLM (via LM Studio) + Memcove build an analytics warehouse.

Unlike the fully-autonomous ``agent_demo.py`` (which needs a strong, tool-capable
model to stay on-plan), this script *orchestrates* the data lifecycle
deterministically and uses the local LLM for the parts it's genuinely good at:

  • inventing the raw datasets (customers / products / orders),
  • authoring the analytical SQL (joins + rollups),
  • narrating the final insights.

Every LLM step has a built-in fallback, so the pipeline always completes with an
impressive result even if the model is weak, slow, or gets unloaded. All data
operations go through the real Memcove MCP tools over Streamable HTTP.

Prereqs:
  1. docker compose up -d
  2. memcove-server                       # MCP server on :8090
  3. LM Studio with a model loaded on :1234   (optional — falls back if absent)
  4. uv sync --extra dev

Run:
  uv run python scripts/pipeline_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MEMCOVE_URL = os.environ.get("MEMCOVE_MCP_URL", "http://localhost:8090/mcp")
TENANT = os.environ.get("MEMCOVE_TENANT", "pipeline_demo")
LM_BASE = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LM_KEY = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")
LM_MODEL = os.environ.get("LMSTUDIO_MODEL")


# --------------------------------------------------------------- LLM (best-effort)

class LLM:
    """Thin LM Studio wrapper that never raises — returns None on any failure."""

    def __init__(self):
        self.client = None
        self.model = None

    async def connect(self) -> None:
        try:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(base_url=LM_BASE, api_key=LM_KEY)
            self.model = LM_MODEL or (await self.client.models.list()).data[0].id
            print(f"LLM: using {self.model}")
        except Exception as exc:  # noqa: BLE001
            print(f"LLM: unavailable ({exc}); using deterministic fallbacks")

    async def text(self, system: str, user: str, temperature: float = 0.4) -> str | None:
        if not self.client:
            return None
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
            )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            print(f"   (LLM call failed: {exc}; using fallback)")
            return None


def extract_json_array(text: str | None) -> list | None:
    """Pull the first JSON array out of an LLM response, tolerating prose around it."""
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, list) and data else None
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------- deterministic seeds

SEED_CUSTOMERS = [
    {"customer_id": 101, "name": "Ada Lovelace", "country": "UK", "signup_month": "2024-01"},
    {"customer_id": 102, "name": "Grace Hopper", "country": "USA", "signup_month": "2024-01"},
    {"customer_id": 103, "name": "Linus Torvalds", "country": "Finland", "signup_month": "2024-02"},
    {"customer_id": 104, "name": "Margaret Hamilton", "country": "USA", "signup_month": "2024-02"},
    {"customer_id": 105, "name": "Alan Turing", "country": "UK", "signup_month": "2024-03"},
    {"customer_id": 106, "name": "Katherine Johnson", "country": "USA", "signup_month": "2024-03"},
    {"customer_id": 107, "name": "Dennis Ritchie", "country": "USA", "signup_month": "2024-04"},
    {"customer_id": 108, "name": "Barbara Liskov", "country": "USA", "signup_month": "2024-04"},
]

SEED_PRODUCTS = [
    {"product_id": 1, "name": "Mechanical Keyboard", "category": "Peripherals", "unit_price": 120.0},
    {"product_id": 2, "name": "4K Monitor", "category": "Displays", "unit_price": 410.0},
    {"product_id": 3, "name": "Noise-cancelling Headset", "category": "Audio", "unit_price": 230.0},
    {"product_id": 4, "name": "USB-C Dock", "category": "Peripherals", "unit_price": 95.0},
    {"product_id": 5, "name": "Ergonomic Chair", "category": "Furniture", "unit_price": 540.0},
    {"product_id": 6, "name": "Webcam Pro", "category": "Peripherals", "unit_price": 85.0},
]


def seed_orders() -> list[dict]:
    # Deterministic but varied: spread across customers, products, and months.
    rows, oid = [], 1000
    months = ["2024-02", "2024-03", "2024-04", "2024-05", "2024-06"]
    for i in range(28):
        cust = SEED_CUSTOMERS[i % len(SEED_CUSTOMERS)]["customer_id"]
        prod = SEED_PRODUCTS[(i * 3) % len(SEED_PRODUCTS)]["product_id"]
        rows.append(
            {
                "order_id": oid + i,
                "customer_id": cust,
                "product_id": prod,
                "qty": (i % 3) + 1,
                "order_month": months[i % len(months)],
            }
        )
    return rows


# ----------------------------------------------------------------- MCP tool calls

def parse(result) -> dict | list:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    parts = [c.text for c in (result.content or []) if getattr(c, "text", None)]
    try:
        return json.loads("\n".join(parts))
    except json.JSONDecodeError:
        return {"raw": "\n".join(parts)}


async def remember(session, name, rows):
    res = await session.call_tool(
        "remember_dataset",
        {"name": name, "mode": "replace",
         "source": {"kind": "inline", "format": "json_records", "records": rows}},
    )
    meta = parse(res)
    n = len(meta.get("schema", [])) if isinstance(meta, dict) else "?"
    print(f"  ✓ remembered '{name}': {len(rows)} rows, {n} columns")


async def derive(session, new_name, sql, fallback_sql):
    """Try the (LLM-authored) SQL; fall back to canonical SQL on any failure."""
    for attempt_sql, tag in [(sql, "llm-sql"), (fallback_sql, "fallback-sql")]:
        if not attempt_sql:
            continue
        res = await session.call_tool(
            "derive_dataset", {"new_name": new_name, "mode": "replace", "sql": attempt_sql}
        )
        out = parse(res)
        if isinstance(out, dict) and out.get("label") == new_name:
            print(f"  ✓ derived '{new_name}' via {tag}")
            return
        print(f"  ! {tag} for '{new_name}' failed: {str(out)[:120]}")
    raise RuntimeError(f"could not derive {new_name}")


async def query(session, sql) -> dict:
    res = await session.call_tool("query_memory", {"sql": sql})
    return parse(res)


# ---------------------------------------------------------------------- pipeline

CANONICAL = {
    "order_facts": (
        "SELECT o.order_id, o.customer_id, p.category, o.order_month, "
        "o.qty * p.unit_price AS revenue "
        "FROM orders o JOIN products p ON o.product_id = p.product_id"
    ),
    "revenue_by_customer": (
        "SELECT c.customer_id, c.name, c.country, sum(f.revenue) AS revenue "
        "FROM order_facts f JOIN customers c ON c.customer_id = f.customer_id "
        "GROUP BY c.customer_id, c.name, c.country"
    ),
    "revenue_by_category": (
        "SELECT category, sum(revenue) AS revenue FROM order_facts GROUP BY category"
    ),
    "monthly_revenue": (
        "SELECT order_month, sum(revenue) AS revenue FROM order_facts "
        "GROUP BY order_month ORDER BY order_month"
    ),
    "top_customers": (
        "SELECT name, country, revenue FROM revenue_by_customer ORDER BY revenue DESC LIMIT 5"
    ),
}

GEN_SYS = "You generate compact, realistic synthetic datasets. Output ONLY a JSON array, no prose."


async def gen_dataset(llm, instruction, fallback) -> list[dict]:
    rows = extract_json_array(await llm.text(GEN_SYS, instruction))
    if rows and isinstance(rows[0], dict):
        return rows
    return fallback


async def run() -> int:
    llm = LLM()
    await llm.connect()

    async with streamablehttp_client(MEMCOVE_URL, headers={"x-memcove-tenant": TENANT}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            print(f"\nTenant: {TENANT}\n")

            print("STEP 1 — invent & remember base datasets")
            customers = await gen_dataset(
                llm,
                "Generate 8 e-commerce customers as a JSON array with keys "
                "customer_id (int 101+), name, country, signup_month (YYYY-MM in 2024).",
                SEED_CUSTOMERS,
            )
            products = await gen_dataset(
                llm,
                "Generate 6 tech products as a JSON array with keys "
                "product_id (int 1+), name, category, unit_price (float).",
                SEED_PRODUCTS,
            )
            orders = await gen_dataset(
                llm,
                f"Generate ~28 orders as a JSON array with keys order_id (int 1000+), "
                f"customer_id (one of {[c['customer_id'] for c in customers]}), "
                f"product_id (one of {[p['product_id'] for p in products]}), "
                f"qty (1-3), order_month (YYYY-MM in 2024). Output ONLY the array.",
                seed_orders(),
            )
            await remember(session, "customers", customers)
            await remember(session, "products", products)
            await remember(session, "orders", orders)

            print("\nSTEP 2 — derive analytics with SQL (joins + rollups)")
            sql_sys = (
                "You write a single Trino SQL SELECT. Reference datasets by bare name. "
                "Output ONLY SQL, no markdown, no prose."
            )
            llm_facts = await llm.text(
                sql_sys,
                "Datasets: orders(order_id, customer_id, product_id, qty, order_month), "
                "products(product_id, name, category, unit_price). Write SELECT joining them to "
                "produce order_id, customer_id, category, order_month, and revenue = qty*unit_price.",
            )
            await derive(session, "order_facts", _clean_sql(llm_facts), CANONICAL["order_facts"])
            await derive(session, "revenue_by_customer", None, CANONICAL["revenue_by_customer"])
            await derive(session, "revenue_by_category", None, CANONICAL["revenue_by_category"])
            await derive(session, "monthly_revenue", None, CANONICAL["monthly_revenue"])
            await derive(session, "top_customers", None, CANONICAL["top_customers"])

            print("\nSTEP 3 — inspect lineage of a derived dataset")
            meta = parse(await session.call_tool("inspect_dataset", {"name": "revenue_by_customer"}))
            print(f"  revenue_by_customer lineage parents: {meta.get('lineage', {}).get('parents')}")

            print("\nSTEP 4 — export the headline result as CSV")
            art = parse(await session.call_tool("export_dataset", {"fmt": "csv", "name": "top_customers"}))
            print(f"  ✓ exported {art.get('row_count')} rows -> {art.get('uri')}")
            print(f"    presigned: {art.get('presigned_url', '')[:90]}…")

            print("\nSTEP 5 — the impressive numbers")
            top = await query(session, "SELECT name, country, revenue FROM top_customers")
            cats = await query(session, "SELECT category, revenue FROM revenue_by_category ORDER BY revenue DESC")
            months = await query(session, "SELECT order_month, revenue FROM monthly_revenue ORDER BY order_month")
            _print_table("Top customers by revenue", top)
            _print_table("Revenue by category", cats)
            _print_table("Monthly revenue trend", months)

            print("\nSTEP 6 — narrative")
            narrative = await llm.text(
                "You are a data analyst. Be concise and concrete (4-6 sentences).",
                "Write an executive summary of these e-commerce results.\n"
                f"Top customers: {top.get('rows')}\n"
                f"Revenue by category: {cats.get('rows')}\n"
                f"Monthly trend: {months.get('rows')}",
            )
            print("\n" + (narrative or _fallback_narrative(top, cats, months)))

    print("\nPIPELINE OK")
    return 0


def _clean_sql(sql: str | None) -> str | None:
    if not sql:
        return None
    s = sql.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("SELECT") :] if "SELECT" in s else s
    return s or None


def _print_table(title: str, result: dict) -> None:
    cols = result.get("columns", [])
    rows = result.get("rows", [])
    print(f"\n  {title}")
    if cols:
        print("    " + " | ".join(str(c) for c in cols))
    for row in rows:
        print("    " + " | ".join(str(v) for v in row))


def _fallback_narrative(top, cats, months) -> str:
    t = top.get("rows") or []
    c = cats.get("rows") or []
    lead = t[0] if t else ["?", "?", 0]
    best_cat = c[0] if c else ["?", 0]
    return (
        f"FINAL: {lead[0]} ({lead[1]}) is the top customer at {lead[2]} in revenue. "
        f"'{best_cat[0]}' is the strongest product category ({best_cat[1]}). "
        f"Revenue is tracked across {len(months.get('rows') or [])} months, showing the trend above."
    )


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

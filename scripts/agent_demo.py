"""Agentic E2E demo: a local LLM (via LM Studio) drives Memcove end to end.

A model running in LM Studio (OpenAI-compatible API) is handed the *real* Memcove
MCP tools and asked to build a mini analytics warehouse autonomously: invent
datasets -> remember_dataset -> query_memory -> derive_dataset (joins/rollups)
-> export_dataset. The MCP tool schemas (names + the rich descriptions) are
bridged straight into OpenAI function-calling, so the model picks tools itself.

Prerequisites
-------------
  1. docker compose up -d                 # lakehouse stack
  2. memcove-server                         # MCP server on :8090 (separate terminal)
  3. LM Studio running a TOOL-CAPABLE model, local server on :1234
  4. uv sync --extra dev

Run
---
  uv run python scripts/agent_demo.py
  uv run python scripts/agent_demo.py --dry-run   # print bridged tool specs only
                                                  # (no LLM needed; validates the bridge)

Environment overrides: MEMCOVE_MCP_URL, MEMCOVE_TENANT, LMSTUDIO_BASE_URL,
LMSTUDIO_API_KEY, LMSTUDIO_MODEL, AGENT_MAX_STEPS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MEMCOVE_URL = os.environ.get("MEMCOVE_MCP_URL", "http://localhost:8090/mcp")
TENANT = os.environ.get("MEMCOVE_TENANT", "agent_demo")
LM_BASE = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LM_KEY = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")
LM_MODEL = os.environ.get("LMSTUDIO_MODEL")  # optional; else auto-detect
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "30"))

SYSTEM_PROMPT = """\
You are a senior data analyst agent with access to Memcove, a persistent data
memory backed by SQL (Trino + Iceberg). You store tabular data as named datasets
and compute over them with SQL — you do NOT keep large tables in your replies.

Rules:
- Invent realistic data yourself and store it with remember_dataset (inline
  json_records). Keep raw tables small (a handful to a few dozen rows).
- Reference datasets by their bare name in SQL, e.g. SELECT * FROM orders.
- Use query_memory to explore; use derive_dataset to PERSIST joined/aggregated
  tables you'll reuse (lineage is tracked automatically).
- Work in clear steps, ONE tool call at a time. After each tool result, decide
  the next step and keep going.
- Do NOT stop until every step is complete AND you have called export_dataset.
- Never reply with an empty message. The ONLY time you reply without a tool call
  is the very end: a single message that begins with 'FINAL:' summarizing the
  insights. Until then, always call a tool.
"""

TASK_PROMPT = """\
Build a mini e-commerce analytics warehouse entirely from data you invent, then
surface some impressive insights. Suggested plan (adapt as you like):

1. Create and remember three base datasets:
   - customers(customer_id, name, country, signup_month)        ~8 rows
   - products(product_id, name, category, unit_price)            ~6 rows
   - orders(order_id, customer_id, product_id, qty, order_month) ~30 rows
2. derive_dataset 'order_facts' = orders joined to products with
   revenue = qty * unit_price (and carry customer_id, category, order_month).
3. derive_dataset 'revenue_by_customer' and 'revenue_by_category' from order_facts.
4. derive_dataset 'monthly_revenue' = revenue per order_month (a trend).
5. Build 'top_customers' = top 5 customers by revenue with their country, and
   export_dataset it as CSV.
6. Summarize: who the top customers are, the best category, and the revenue trend.

Make the numbers internally consistent and interesting. Only after the CSV
export succeeds, reply with a 'FINAL:' message describing the findings. Go.
"""


def mcp_tools_to_openai(tools) -> list[dict]:
    """Bridge MCP tool definitions into OpenAI function-calling specs."""
    specs = []
    for t in tools:
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            }
        )
    return specs


def result_to_text(result) -> str:
    """Flatten an MCP CallToolResult into a compact string for the model."""
    if getattr(result, "structuredContent", None):
        text = json.dumps(result.structuredContent, default=str)
    else:
        parts = [c.text for c in (result.content or []) if getattr(c, "text", None)]
        text = "\n".join(parts)
    if getattr(result, "isError", False):
        text = "ERROR: " + text
    return text[:4000]  # keep tool outputs from blowing up context


def summarize_args(args: dict) -> str:
    """One-line, readable view of tool args (truncate big inline payloads)."""
    shown = {}
    for k, v in args.items():
        s = json.dumps(v, default=str)
        shown[k] = (s[:80] + "…") if len(s) > 80 else v
    return json.dumps(shown, default=str)


async def complete(client, model, messages, tools, force: bool):
    """One chat turn. `force` => tool_choice='required' (weak models won't stall),
    with a graceful fallback to 'auto' for servers that reject 'required'."""
    choice = "required" if force else "auto"
    try:
        resp = await client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice=choice, temperature=0.2
        )
    except Exception:  # noqa: BLE001 - server may not support tool_choice='required'
        resp = await client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto", temperature=0.2
        )
    return resp.choices[0].message


async def run(dry_run: bool) -> int:
    async with streamablehttp_client(MEMCOVE_URL, headers={"x-memcove-tenant": TENANT}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            oai_tools = mcp_tools_to_openai(tools)

            if dry_run:
                print(f"Bridged {len(oai_tools)} Memcove tools -> OpenAI function specs:\n")
                for spec in oai_tools:
                    fn = spec["function"]
                    params = list(fn["parameters"].get("properties", {}).keys())
                    print(f"• {fn['name']}({', '.join(params)})")
                    print(f"    {fn['description'].splitlines()[0]}")
                return 0

            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=LM_BASE, api_key=LM_KEY)
            model = LM_MODEL
            if not model:
                try:
                    model = (await client.models.list()).data[0].id
                except Exception:  # noqa: BLE001
                    model = "local-model"
            print(f"Using LM Studio model: {model}\nTenant: {TENANT}\n")

            messages: list[dict] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": TASK_PROMPT},
            ]

            # Weak local models stall with tool_choice="auto" (they emit empty
            # turns). Force a tool call every step UNTIL the export lands; only
            # then switch to "auto" so the model can write the FINAL: summary.
            export_done = False
            final_prompted = False
            for step in range(1, MAX_STEPS + 1):
                msg = await complete(client, model, messages, oai_tools, force=not export_done)

                assistant: dict = {"role": "assistant", "content": msg.content or None}
                if msg.tool_calls:
                    assistant["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ]
                messages.append(assistant)

                if not msg.tool_calls:
                    content = (msg.content or "").strip()
                    if export_done and len(content) > 40:
                        label = "Agent finished" if content.upper().startswith("FINAL") else "Agent done"
                        print(f"\n=== {label} (step {step}) ===\n{content}")
                        return 0
                    # Stalled before finishing — push it to make a tool call.
                    print(f"[{step}] (no tool call; pushing to continue)")
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Not done yet — make exactly ONE tool call now to continue "
                                "(remaining: orders, the derived joins/rollups, then export_dataset)."
                            ),
                        }
                    )
                    continue

                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        text = f"ERROR: could not parse arguments: {exc}"
                        print(f"[{step}] {name}  <bad args>")
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
                        continue

                    print(f"[{step}] → {name}({summarize_args(args)})")
                    try:
                        result = await session.call_tool(name, args)
                        text = result_to_text(result)
                    except Exception as exc:  # noqa: BLE001 - feed errors back to the model
                        text = f"ERROR: {exc}"
                    print(f"      ⤷ {text[:200]}")
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
                    if name == "export_dataset" and not text.startswith("ERROR"):
                        export_done = True

                if export_done and not final_prompted:
                    final_prompted = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The export is complete. Now reply with a single message beginning "
                                "'FINAL:' that summarizes the top customers, the best product category, "
                                "and the monthly revenue trend. Do not call any more tools."
                            ),
                        }
                    )

            print(f"\n=== Stopped after {MAX_STEPS} steps (max) ===")
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic Memcove demo via LM Studio")
    parser.add_argument("--dry-run", action="store_true", help="print bridged tool specs and exit")
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())

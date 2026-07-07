"""DCF valuation pipeline — value public companies with Memcove.

A model-free pipeline that pulls real financial-statement data from yfinance (cash-flow
statement + balance-sheet/market snapshot), loads it into Memcove, and runs a
**discounted-cash-flow** valuation as a multi-hop SQL DAG entirely inside Trino:

    fundamentals ─▶ fcf_history ─▶ fcf_growth ─┐
    market ────────────────────────────────────┼─▶ dcf_base ─▶ proj_fcf ─▶ valuation ─▶ fair_value
    dcf_params ─────────────────────────────────┘         (× proj_years)

Method (a deliberately simple, transparent enterprise DCF — see caveats in the README):
  FCF        = operating cash flow + capex            (capex is negative)
  growth     = historical FCF CAGR, clamped
  disc_rate  = CAPM cost of equity  rf + beta·erp     (used as the WACC proxy)
  EV         = Σ FCFₙ/(1+r)ⁿ  +  [FCF_N·(1+g_term)/(r−g_term)]/(1+r)ᴺ
  equity     = EV − net debt        fair/share = equity / shares      upside = fair/price − 1

Run (stack up: `docker compose up -d --wait`; `uv sync --extra bench`):

    uv run memcove-dcf
    uv run memcove-dcf --tickers AAPL,MSFT,GOOGL --proj-years 7 --term-growth 0.03
    uv run memcove-dcf --synthetic        # offline, deterministic
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from memcove.benchmarks.finance import Bench, count_rows, reset_tenant
from memcove.core.tenancy import normalize_tenant
from memcove.tools import artifacts, derive, ingest, query

# Large-cap universe with generally positive free cash flow (US, USD-reporting).
UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "META", "NVDA", "AVGO", "JPM", "JNJ", "PG", "KO",
    "PEP", "HD", "MCD", "V", "MA", "UNH", "XOM", "CVX", "WMT", "COST",
]

# --- the DCF DAG: (label, description, sql) -------------------------------------------
DERIVATIONS = [
    (
        "fcf_history",
        "free cash flow per fiscal year (OCF + CapEx)",
        # capex is reported negative, so OCF + capex = OCF - capital spending.
        "SELECT ticker, fiscal_year, ocf + capex AS fcf FROM fundamentals",
    ),
    (
        "fcf_growth",
        "base FCF + first FCF + count of years, per ticker (MIN_BY/MAX_BY)",
        """
        SELECT ticker,
               max_by(fcf, fiscal_year) AS base_fcf,
               min_by(fcf, fiscal_year) AS first_fcf,
               count(*) AS years
        FROM fcf_history
        GROUP BY ticker
        """,
    ),
    (
        "dcf_base",
        "per-company inputs: clamped growth, CAPM discount rate, net debt (JOIN market + params)",
        """
        SELECT g.ticker,
               g.base_fcf,
               greatest(-0.02, least(0.12,
                   CASE WHEN g.first_fcf > 0 AND g.base_fcf > 0 AND g.years > 1
                        THEN power(g.base_fcf / g.first_fcf, 1.0 / (g.years - 1)) - 1
                        ELSE 0.04 END)) AS growth,
               greatest(p.rf + m.beta * p.erp, p.term_growth + 0.02) AS disc_rate,
               p.term_growth,
               p.n_years,
               m.shares,
               m.price,
               m.total_debt - m.cash AS net_debt
        FROM fcf_growth g
        JOIN market m ON m.ticker = g.ticker
        CROSS JOIN dcf_params p
        WHERE g.base_fcf > 0
        """,
    ),
    (
        "proj_fcf",
        "explicit-horizon projected + discounted FCF (CROSS JOIN proj_years, POWER)",
        """
        SELECT b.ticker, y.n,
               b.base_fcf * power(1 + b.growth, y.n) AS fcf_n,
               b.base_fcf * power(1 + b.growth, y.n) / power(1 + b.disc_rate, y.n) AS pv_fcf
        FROM dcf_base b
        JOIN proj_years y ON y.n <= b.n_years
        """,
    ),
    (
        "valuation",
        "PV of explicit FCF + discounted terminal value (Gordon growth), 2-input JOIN",
        """
        SELECT b.ticker,
               e.pv_explicit,
               (b.base_fcf * power(1 + b.growth, b.n_years) * (1 + b.term_growth)
                    / (b.disc_rate - b.term_growth))
                   / power(1 + b.disc_rate, b.n_years) AS pv_terminal,
               b.net_debt, b.shares, b.price
        FROM dcf_base b
        JOIN (SELECT ticker, sum(pv_fcf) AS pv_explicit FROM proj_fcf GROUP BY ticker) e
             ON e.ticker = b.ticker
        """,
    ),
    (
        "fair_value",
        "enterprise → equity → per-share fair value and upside vs market price",
        """
        SELECT ticker,
               pv_explicit + pv_terminal AS enterprise_value,
               pv_explicit + pv_terminal - net_debt AS equity_value,
               (pv_explicit + pv_terminal - net_debt) / shares AS fair_value_per_share,
               price,
               (pv_explicit + pv_terminal - net_debt) / shares / nullif(price, 0) - 1 AS upside
        FROM valuation
        """,
    ),
]


# ------------------------------------------------------------------ data loading

def load_data(tickers: list[str], synthetic: bool) -> tuple[list[dict], list[dict]]:
    """Return (fundamentals rows, market rows) from yfinance, or synthetic fallback."""
    if not synthetic:
        try:
            fund, market = _fetch_yfinance(tickers)
            if len(market) >= 3:
                print(f"  loaded fundamentals for {len(market)}/{len(tickers)} tickers from yfinance")
                return fund, market
            print("  ! too few tickers returned; falling back to synthetic")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! yfinance fundamentals failed ({exc}); using synthetic")
    return _synthetic(tickers)


def _row(df, *names):
    import pandas as pd
    for n in names:
        if n in df.index:
            return df.loc[n]
    return pd.Series(dtype=float)


def _fetch_yfinance(tickers: list[str]) -> tuple[list[dict], list[dict]]:
    import pandas as pd
    import yfinance as yf

    fund, market = [], []
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            cf = tk.cashflow
            info = tk.info
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
            shares = info.get("sharesOutstanding")
            if cf is None or cf.empty or not price or not shares:
                continue
            ocf_row = _row(cf, "Operating Cash Flow", "Total Cash From Operating Activities",
                           "Cash Flow From Continuing Operating Activities")
            capex_row = _row(cf, "Capital Expenditure", "Capital Expenditures")
            rows = []
            for col in cf.columns:
                ocf = ocf_row.get(col)
                if ocf is None or pd.isna(ocf):
                    continue
                capex = capex_row.get(col)
                capex = 0.0 if capex is None or pd.isna(capex) else float(capex)
                rows.append({"ticker": t, "fiscal_year": int(col.year),
                             "ocf": float(ocf), "capex": capex})
            if not rows:
                continue
            fund.extend(rows)
            market.append({
                "ticker": t, "price": float(price), "shares": float(shares),
                "beta": float(info.get("beta") or 1.0),
                "total_debt": float(info.get("totalDebt") or 0.0),
                "cash": float(info.get("totalCash") or 0.0),
            })
        except Exception:  # noqa: BLE001 - skip a bad ticker, keep going
            continue
    return fund, market


def _synthetic(tickers: list[str]) -> tuple[list[dict], list[dict]]:
    """Deterministic plausible fundamentals + market snapshot (offline)."""
    import numpy as np

    print(f"  synthesizing fundamentals for {len(tickers)} tickers")
    fund, market = [], []
    for t in tickers:
        rng = np.random.default_rng(abs(hash(t)) % (2**32))
        ocf0 = float(rng.uniform(2e9, 8e10))          # base operating cash flow
        g = float(rng.uniform(0.03, 0.15))            # historical growth
        capex_frac = float(rng.uniform(0.15, 0.5))    # capex as a share of OCF
        for i, yr in enumerate(range(2020, 2024)):
            ocf = ocf0 * (1 + g) ** i
            fund.append({"ticker": t, "fiscal_year": yr, "ocf": round(ocf, 0),
                         "capex": round(-ocf * capex_frac, 0)})
        shares = float(rng.uniform(3e8, 8e9))
        fcf_latest = ocf0 * (1 + g) ** 3 * (1 - capex_frac)
        # anchor price loosely to a ~25x FCF multiple so upside isn't absurd
        price = max(5.0, round(fcf_latest * rng.uniform(15, 35) / shares, 2))
        market.append({
            "ticker": t, "price": price, "shares": round(shares, 0),
            "beta": round(float(rng.uniform(0.7, 1.6)), 2),
            "total_debt": round(float(rng.uniform(0, 5e10)), 0),
            "cash": round(float(rng.uniform(0, 3e10)), 0),
        })
    return fund, market


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description="Memcove DCF valuation pipeline")
    ap.add_argument("--tickers", default="", help="comma list (default: ~20 US large caps)")
    ap.add_argument("--proj-years", type=int, default=5, help="explicit forecast horizon")
    ap.add_argument("--rf", type=float, default=0.043, help="risk-free rate")
    ap.add_argument("--erp", type=float, default=0.05, help="equity risk premium")
    ap.add_argument("--term-growth", type=float, default=0.025, help="terminal growth rate")
    ap.add_argument("--tenant", default="dcf")
    ap.add_argument("--synthetic", action="store_true", help="skip yfinance, use synthetic data")
    ap.add_argument("--out-dir", default="benchmark-output")
    args = ap.parse_args()

    tenant = normalize_tenant(args.tenant)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or UNIVERSE
    out_dir = Path(args.out_dir)
    bench = Bench()

    print(f"\n== Memcove DCF pipeline ==  tenant={tenant}  tickers={len(tickers)}  "
          f"proj_years={args.proj_years}  rf={args.rf}  erp={args.erp}  g_term={args.term_growth}\n")

    # 1. Load real financial-statement data.
    print("-- load --")
    fundamentals, market = load_data(tickers, args.synthetic)
    n = len({m["ticker"] for m in market})
    print(f"  {len(fundamentals)} statement-years across {n} companies\n")

    # 2. Ingest into Memcove (all small — inline).
    print("-- ingest --")
    from memcove.core import registry
    registry.init_db()
    reset_tenant(tenant)

    def ingest_inline(label, records):
        t0 = time.perf_counter()
        ingest.ingest_object(
            tenant, label, {"kind": "inline", "format": "json_records", "records": records},
            mode="replace",
        )
        bench.record(label, "ingest", len(records), time.perf_counter() - t0)

    ingest_inline("fundamentals", fundamentals)
    ingest_inline("market", market)
    ingest_inline("proj_years", [{"n": i} for i in range(1, args.proj_years + 1)])
    ingest_inline("dcf_params", [{
        "rf": args.rf, "erp": args.erp, "term_growth": args.term_growth,
        "n_years": args.proj_years,
    }])

    # 3. The DCF DAG.
    print("\n-- derive (DCF DAG) --")
    for label, desc, sql in DERIVATIONS:
        t0 = time.perf_counter()
        derive.derive_object(tenant, label, sql, mode="replace")
        bench.record(label, "derive", count_rows(tenant, label), time.perf_counter() - t0, desc)

    # 4. The valuation leaderboard.
    print("\n-- valuation (fair value vs market price) --")
    res = query.run_query(
        tenant,
        "SELECT ticker, round(price, 2) AS price, "
        "round(fair_value_per_share, 2) AS fair_value, round(100 * upside, 1) AS upside_pct "
        "FROM fair_value ORDER BY upside DESC",
        limit=len(tickers),
    )
    _print_leaderboard(res)

    # 5. Export the valuation table.
    t0 = time.perf_counter()
    ref = artifacts.export_artifact(tenant, fmt="csv", label="fair_value")
    bench.record("fair_value.csv", "export", ref.row_count, time.perf_counter() - t0, ref.uri)

    _write_result(bench, res, tenant, args, out_dir)


def _print_leaderboard(res) -> None:
    cols = res.columns
    print(f"  {'ticker':<8}{'price':>12}{'fair_value':>14}{'upside_%':>11}")
    print("  " + "-" * 43)
    for row in res.rows:
        d = dict(zip(cols, row))
        print(f"  {d['ticker']:<8}{d['price']:>12,.2f}{d['fair_value']:>14,.2f}{d['upside_pct']:>10}%")


def _write_result(bench, res, tenant, args, out_dir: Path) -> None:
    total = sum(p.seconds for p in bench.phases)
    print(f"\n== summary ==  companies valued: {res.row_count}  wall: {total:.1f}s")
    out = out_dir / "results"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"dcf_{res.row_count}co.json"
    path.write_text(json.dumps({
        "tenant": tenant, "proj_years": args.proj_years,
        "rf": args.rf, "erp": args.erp, "term_growth": args.term_growth,
        "valuations": [dict(zip(res.columns, r)) for r in res.rows],
        "phases": [vars(p) for p in bench.phases],
    }, indent=2, default=str))
    print(f"  results written : {path}")


if __name__ == "__main__":
    main()

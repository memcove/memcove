"""DCF valuation pipeline — value public companies with Memcove.

A model-free pipeline that pulls real financial-statement data from yfinance (cash-flow +
income statement + market/balance-sheet snapshot), loads it into Memcove, and runs a
**discounted-cash-flow** valuation as a multi-hop SQL DAG entirely inside Trino:

    fundamentals ─▶ fcf_history ─▶ fcf_growth ─┐
    fundamentals ─▶ discount_rate ─────────────┤
    market ────────────────────────────────────┼─▶ dcf_base ─▶ proj_fcf ─▶ valuation ─▶ fair_value
    dcf_params ─────────────────────────────────┘         (× proj_years)

Two methods (`--method`):

  fcff  (default) — proper enterprise DCF:
      FCFF      = OCF + interest·(1−tax) + capex        (unlevered free cash flow)
      disc_rate = WACC = (E/V)·Re + (D/V)·Rd·(1−tax)    (Re via CAPM = rf + beta·erp)
  simple — quick levered proxy:
      FCF       = OCF + capex
      disc_rate = CAPM cost of equity (Re)

Both: growth = clamped historical FCF CAGR; terminal value via Gordon growth;
EV = Σ FCFₙ/(1+r)ⁿ + PV(terminal); equity = EV − net debt; fair/share = equity/shares.

Run (stack up: `docker compose up -d --wait`; `uv sync --extra bench`):

    uv run memcove-dcf                       # value the built-in ~20 US large caps
    uv run memcove-dcf AAPL                   # one ticker → detailed breakdown
    uv run memcove-dcf AAPL MSFT GOOGL        # several → leaderboard
    uv run memcove-dcf NVDA --method simple --proj-years 7
    uv run memcove-dcf --synthetic            # offline, deterministic
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

# Per-method free-cash-flow definition (capex is reported negative → adding it subtracts spend).
FCF_EXPR = {
    "simple": "ocf + capex",
    "fcff": "ocf + abs(interest) * (1 - tax_rate) + capex",
}

# Per-method discount rate, both emitting a uniform schema so downstream steps are shared.
DISCOUNT_SQL = {
    # cost of equity only
    "simple": """
        SELECT m.ticker,
               p.rf + m.beta * p.erp AS re,
               CAST(NULL AS double) AS rd_after_tax,
               1.0 AS equity_weight,
               p.rf + m.beta * p.erp AS wacc,
               greatest(p.term_growth + 0.02, p.rf + m.beta * p.erp) AS disc_rate
        FROM market m CROSS JOIN dcf_params p
    """,
    # full WACC = We·Re + Wd·Rd·(1−tax), using latest-year interest/tax and market weights
    "fcff": """
        SELECT ticker, re, rd_after_tax, equity_weight,
               equity_weight * re + (1 - equity_weight) * rd_after_tax AS wacc,
               greatest(term_growth + 0.02,
                        equity_weight * re + (1 - equity_weight) * rd_after_tax) AS disc_rate
        FROM (
            SELECT m.ticker,
                   p.rf + m.beta * p.erp AS re,
                   (CASE WHEN m.total_debt > 0
                         THEN greatest(0.01, least(0.12, l.interest / m.total_debt))
                         ELSE p.rf + 0.01 END) * (1 - l.tax_rate) AS rd_after_tax,
                   (m.price * m.shares) / (m.price * m.shares + m.total_debt) AS equity_weight,
                   p.term_growth
            FROM market m
            JOIN (SELECT ticker,
                         max_by(abs(interest), fiscal_year) AS interest,
                         max_by(tax_rate, fiscal_year) AS tax_rate
                  FROM fundamentals GROUP BY ticker) l ON l.ticker = m.ticker
            CROSS JOIN dcf_params p
        )
    """,
}


def build_dag(method: str) -> list[tuple[str, str, str]]:
    """The DCF DAG for a method. Only fcf_history + discount_rate vary; the rest is shared."""
    return [
        (
            "fcf_history",
            f"free cash flow per fiscal year ({FCF_EXPR[method]})",
            f"SELECT ticker, fiscal_year, {FCF_EXPR[method]} AS fcf FROM fundamentals",
        ),
        (
            "fcf_growth",
            "base FCF + first FCF + years, per ticker (MIN_BY/MAX_BY)",
            """
            SELECT ticker,
                   max_by(fcf, fiscal_year) AS base_fcf,
                   min_by(fcf, fiscal_year) AS first_fcf,
                   count(*) AS years
            FROM fcf_history GROUP BY ticker
            """,
        ),
        (
            "discount_rate",
            "per-company discount rate (WACC for fcff, cost of equity for simple)",
            DISCOUNT_SQL[method],
        ),
        (
            "dcf_base",
            "per-company inputs: clamped growth, discount rate, net debt (3-way JOIN + params)",
            """
            SELECT g.ticker,
                   g.base_fcf,
                   greatest(-0.02, least(0.12,
                       CASE WHEN g.first_fcf > 0 AND g.base_fcf > 0 AND g.years > 1
                            THEN power(g.base_fcf / g.first_fcf, 1.0 / (g.years - 1)) - 1
                            ELSE 0.04 END)) AS growth,
                   d.disc_rate, d.wacc, d.re, d.rd_after_tax, d.equity_weight,
                   p.term_growth, p.n_years,
                   m.shares, m.price, m.total_debt - m.cash AS net_debt
            FROM fcf_growth g
            JOIN market m ON m.ticker = g.ticker
            JOIN discount_rate d ON d.ticker = g.ticker
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
                   b.net_debt, b.shares, b.price, b.disc_rate, b.growth, b.base_fcf,
                   b.re, b.rd_after_tax, b.equity_weight
            FROM dcf_base b
            JOIN (SELECT ticker, sum(pv_fcf) AS pv_explicit FROM proj_fcf GROUP BY ticker) e
                 ON e.ticker = b.ticker
            """,
        ),
        (
            "fair_value",
            "enterprise → equity → per-share fair value and upside vs market price",
            """
            SELECT ticker, base_fcf, growth, re, rd_after_tax, equity_weight, disc_rate,
                   pv_explicit, pv_terminal,
                   pv_explicit + pv_terminal AS enterprise_value,
                   net_debt, shares,
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
            if len(market) >= max(1, min(3, len(tickers))):
                print(f"  loaded fundamentals for {len(market)}/{len(tickers)} tickers from yfinance")
                return fund, market
            print("  ! too few tickers returned; falling back to synthetic")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! yfinance fundamentals failed ({exc}); using synthetic")
    return _synthetic(tickers)


def _row(df, *names):
    import pandas as pd
    for n in names:
        if df is not None and n in df.index:
            return df.loc[n]
    return pd.Series(dtype=float)


def _by_year(row, year):
    """Value in a statement Series (indexed by period-end dates) for a given fiscal year."""
    for col in row.index:
        if getattr(col, "year", None) == year:
            return row.get(col)
    return None


def _tax_rate(income, year: int) -> float:
    import pandas as pd
    direct = _by_year(_row(income, "Tax Rate For Calcs"), year)
    if direct is not None and not pd.isna(direct) and 0 <= direct <= 0.6:
        return float(direct)
    tax = _by_year(_row(income, "Tax Provision", "Income Tax Expense"), year)
    pretax = _by_year(_row(income, "Pretax Income", "Income Before Tax"), year)
    if tax is not None and pretax not in (None, 0) and not pd.isna(tax) and not pd.isna(pretax):
        return min(0.40, max(0.05, float(tax) / float(pretax)))
    return 0.21  # US statutory default


def _fetch_yfinance(tickers: list[str]) -> tuple[list[dict], list[dict]]:
    import pandas as pd
    import yfinance as yf

    fund, market = [], []
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            cf, income, info = tk.cashflow, tk.income_stmt, tk.info
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
            shares = info.get("sharesOutstanding")
            if cf is None or cf.empty or not price or not shares:
                continue
            ocf_row = _row(cf, "Operating Cash Flow", "Total Cash From Operating Activities",
                           "Cash Flow From Continuing Operating Activities")
            capex_row = _row(cf, "Capital Expenditure", "Capital Expenditures")
            interest_row = _row(income, "Interest Expense", "Interest Expense Non Operating")
            rows = []
            for col in cf.columns:
                ocf = ocf_row.get(col)
                if ocf is None or pd.isna(ocf):
                    continue
                yr = int(col.year)
                capex = capex_row.get(col)
                interest = _by_year(interest_row, yr)
                rows.append({
                    "ticker": t, "fiscal_year": yr, "ocf": float(ocf),
                    "capex": 0.0 if capex is None or pd.isna(capex) else float(capex),
                    "interest": 0.0 if interest is None or pd.isna(interest) else abs(float(interest)),
                    "tax_rate": _tax_rate(income, yr),
                })
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
        debt = float(rng.uniform(0, 5e10))
        tax_rate = round(float(rng.uniform(0.12, 0.25)), 3)
        for i, yr in enumerate(range(2020, 2024)):
            ocf = ocf0 * (1 + g) ** i
            fund.append({
                "ticker": t, "fiscal_year": yr, "ocf": round(ocf, 0),
                "capex": round(-ocf * capex_frac, 0),
                "interest": round(debt * float(rng.uniform(0.03, 0.06)), 0),
                "tax_rate": tax_rate,
            })
        shares = float(rng.uniform(3e8, 8e9))
        fcf_latest = ocf0 * (1 + g) ** 3 * (1 - capex_frac)
        price = max(5.0, round(fcf_latest * rng.uniform(15, 35) / shares, 2))
        market.append({
            "ticker": t, "price": price, "shares": round(shares, 0),
            "beta": round(float(rng.uniform(0.7, 1.6)), 2),
            "total_debt": round(debt, 0),
            "cash": round(float(rng.uniform(0, 3e10)), 0),
        })
    return fund, market


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description="Memcove DCF valuation pipeline")
    ap.add_argument("tickers", nargs="*", help="one or more tickers (default: ~20 US large caps)")
    ap.add_argument("--method", choices=["fcff", "simple"], default="fcff",
                    help="fcff = unlevered FCF discounted at WACC (default); simple = FCFE proxy at Re")
    ap.add_argument("--proj-years", type=int, default=5, help="explicit forecast horizon")
    ap.add_argument("--rf", type=float, default=0.043, help="risk-free rate")
    ap.add_argument("--erp", type=float, default=0.05, help="equity risk premium")
    ap.add_argument("--term-growth", type=float, default=0.025, help="terminal growth rate")
    ap.add_argument("--tenant", default="dcf")
    ap.add_argument("--synthetic", action="store_true", help="skip yfinance, use synthetic data")
    ap.add_argument("--out-dir", default="benchmark-output")
    args = ap.parse_args()

    tenant = normalize_tenant(args.tenant)
    tickers = [t.strip().upper() for t in args.tickers] or UNIVERSE
    out_dir = Path(args.out_dir)
    bench = Bench()

    print(f"\n== Memcove DCF pipeline ==  method={args.method}  tenant={tenant}  "
          f"tickers={len(tickers)}  proj_years={args.proj_years}  "
          f"rf={args.rf}  erp={args.erp}  g_term={args.term_growth}\n")

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
    for label, desc, sql in build_dag(args.method):
        t0 = time.perf_counter()
        derive.derive_object(tenant, label, sql, mode="replace")
        bench.record(label, "derive", count_rows(tenant, label), time.perf_counter() - t0, desc)

    # 4. Report: a single ticker gets a full breakdown; several get a leaderboard.
    board = query.run_query(
        tenant,
        "SELECT ticker, round(price, 2) AS price, "
        "round(fair_value_per_share, 2) AS fair_value, round(100 * disc_rate, 1) AS disc_pct, "
        "round(100 * upside, 1) AS upside_pct FROM fair_value ORDER BY upside DESC",
        limit=len(tickers),
    )
    if len(tickers) == 1:
        _print_detail(tenant, tickers[0], args.method)
    else:
        _print_leaderboard(board, args.method)

    # 5. Export the valuation table.
    t0 = time.perf_counter()
    ref = artifacts.export_artifact(tenant, fmt="csv", label="fair_value")
    bench.record("fair_value.csv", "export", ref.row_count, time.perf_counter() - t0, ref.uri)

    _write_result(bench, board, tenant, args, out_dir)


def _fmt_money(x) -> str:
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e9:
        return f"${x / 1e9:,.1f}B"
    if ax >= 1e6:
        return f"${x / 1e6:,.1f}M"
    return f"${x:,.0f}"


def _print_leaderboard(res, method: str) -> None:
    disc_label = "wacc_%" if method == "fcff" else "coe_%"
    print(f"\n-- valuation ({method}): fair value vs market price --")
    print(f"  {'ticker':<8}{'price':>11}{'fair_value':>13}{disc_label:>9}{'upside_%':>11}")
    print("  " + "-" * 52)
    for row in res.rows:
        d = dict(zip(res.columns, row))
        print(f"  {d['ticker']:<8}{d['price']:>11,.2f}{d['fair_value']:>13,.2f}"
              f"{d['disc_pct']:>8}%{d['upside_pct']:>10}%")


def _print_detail(tenant: str, ticker: str, method: str) -> None:
    res = query.run_query(tenant, "SELECT * FROM fair_value", limit=5)
    if res.row_count == 0:
        print(f"\n  No positive-FCF valuation for {ticker} — the FCFF DCF needs positive free "
              f"cash flow (e.g. banks and negative-FCF firms are excluded).")
        return
    d = dict(zip(res.columns, res.rows[0]))
    rate_name = "WACC (discount rate)" if method == "fcff" else "cost of equity (discount rate)"
    print(f"\n== {ticker} DCF — method={method} ==")
    rows = [
        ("base FCF (latest)", _fmt_money(d["base_fcf"])),
        ("FCF growth (CAGR)", f"{100 * d['growth']:.1f}%"),
        ("cost of equity (Re)", f"{100 * d['re']:.1f}%"),
    ]
    if method == "fcff":
        rows += [
            ("after-tax cost of debt", "n/a" if d["rd_after_tax"] is None else f"{100 * d['rd_after_tax']:.1f}%"),
            ("equity weight (E/V)", f"{100 * d['equity_weight']:.0f}%"),
        ]
    rows += [
        (rate_name, f"{100 * d['disc_rate']:.1f}%"),
        ("—", ""),
        ("PV explicit FCF", _fmt_money(d["pv_explicit"])),
        ("PV terminal value", _fmt_money(d["pv_terminal"])),
        ("enterprise value", _fmt_money(d["enterprise_value"])),
        ("− net debt", _fmt_money(d["net_debt"])),
        ("= equity value", _fmt_money(d["equity_value"])),
        ("÷ shares", f"{d['shares'] / 1e9:.2f}B"),
        ("—", ""),
        ("fair value / share", f"${d['fair_value_per_share']:,.2f}"),
        ("market price", f"${d['price']:,.2f}"),
        ("upside", f"{100 * d['upside']:+.1f}%"),
    ]
    for name, val in rows:
        print("  " + ("-" * 40 if name == "—" else f"{name:<26}{val:>14}"))


def _write_result(bench, board, tenant, args, out_dir: Path) -> None:
    total = sum(p.seconds for p in bench.phases)
    print(f"\n== summary ==  method={args.method}  companies valued: {board.row_count}  "
          f"wall: {total:.1f}s")
    out = out_dir / "results"
    out.mkdir(parents=True, exist_ok=True)
    tag = args.tickers[0].lower() if len(args.tickers) == 1 else f"{board.row_count}co"
    path = out / f"dcf_{args.method}_{tag}.json"
    path.write_text(json.dumps({
        "method": args.method, "tenant": tenant, "proj_years": args.proj_years,
        "rf": args.rf, "erp": args.erp, "term_growth": args.term_growth,
        "valuations": [dict(zip(board.columns, r)) for r in board.rows],
        "phases": [vars(p) for p in bench.phases],
    }, indent=2, default=str))
    print(f"  results written : {path}")


if __name__ == "__main__":
    main()

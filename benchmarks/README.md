# Memcove example workloads

Two **deterministic, model-free** workloads that drive the real Memcove tools with real
market data — no LLM, a fixed plan stands in for the model, so runs are reproducible and
isolate the compute/storage engine (Trino + Iceberg + the registry) from the MCP
transport. Both fall back to deterministic synthetic data (`--synthetic`) so they run
offline, and both live in `memcove.benchmarks`.

```bash
docker compose up -d --wait          # Memcove stack (Trino, Iceberg, MinIO, Postgres)
uv sync --extra bench                # yfinance + pandas
```

Outputs (price cache, result JSON) go to `./benchmark-output/` — override with `--out-dir`.
Outside the repo (after `pip install memcove[bench]`) the commands are on your PATH.

---

## 1. `memcove-bench` — throughput benchmark

Ingests real daily OHLCV and builds a **multi-hop DAG** of derived datasets, timing every
step.

```bash
uv run memcove-bench --years 8 --replicate 4
uv run memcove-bench --years 10 --replicate 10 --heavy-corr   # bigger + heaviest hop
uv run memcove-bench --synthetic                              # offline
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--years` | 8 | history depth (rows scale linearly) |
| `--replicate` | 4 | clone each ticker N× with a perturbed price path — real seed, synthetic scale |
| `--heavy-corr` | off | add the within-sector return-correlation self-join (heaviest hop) |
| `--synthetic` | off | skip yfinance; use GBM data |
| `--tickers` | — | comma list to override the ~44-name universe |

**DAG:** `daily_returns` (LAG) → `rolling_vol` (STDDEV window) / `rolling_ma` (AVG windows)
→ `returns_by_sector` (join+GROUP BY) → `sector_vol_monthly`, `monthly_perf`
(MIN_BY/MAX_BY), `top_movers` (RANK), `pairwise_corr` (self-join, `--heavy-corr`), and a
5-way `signal` join with the deepest lineage.

**Sample** (real data, 8y, `--replicate 4 --heavy-corr`): 220 tickers, 432,580 price rows,
**1.77M rows materialized in ~15s**; ingest ~395k rows/s.

---

## 2. `memcove-dcf` — DCF valuation pipeline

Pulls real financial-statement data (cash-flow + income statement + market/balance-sheet
snapshot from yfinance), loads it into Memcove, and runs a **discounted-cash-flow**
valuation as a multi-hop SQL DAG entirely inside Trino. Pass tickers as positional
arguments — one ticker prints a full breakdown, several print a leaderboard.

```bash
uv run memcove-dcf                              # value the built-in ~20 US large caps
uv run memcove-dcf AAPL                          # one ticker → detailed breakdown
uv run memcove-dcf AAPL MSFT GOOGL               # several → leaderboard
uv run memcove-dcf NVDA --method simple --proj-years 7
uv run memcove-dcf --synthetic                   # offline, deterministic
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `<tickers>` | universe | positional: one or more tickers to value |
| `--method` | `fcff` | `fcff` (unlevered FCF at WACC) or `simple` (FCFE proxy at cost of equity) |
| `--proj-years` | 5 | explicit forecast horizon |
| `--rf` | 0.043 | risk-free rate |
| `--erp` | 0.05 | equity risk premium (cost of equity = `rf + beta·erp`) |
| `--term-growth` | 0.025 | terminal growth rate (Gordon growth) |
| `--synthetic` | off | skip yfinance; use synthetic fundamentals |

**DAG:** `fundamentals` → `fcf_history` → `fcf_growth` (historical CAGR) → `discount_rate`
(WACC or cost of equity) → `dcf_base` (3-way join: growth, discount rate, net debt) →
`proj_fcf` (× `proj_years`, POWER discounting) → `valuation` (Σ PV + terminal value) →
`fair_value` (enterprise → equity → per-share, upside vs price).

**Two methods:**

```
fcff (default) — proper enterprise DCF
  FCFF      = OCF + interest·(1−tax) + capex          (unlevered)
  disc_rate = WACC = (E/V)·Re + (D/V)·Rd·(1−tax)      (Re = rf + beta·erp via CAPM)

simple — quick levered proxy
  FCF       = OCF + capex                             (capex is negative)
  disc_rate = CAPM cost of equity (Re)

both:  growth = historical FCF CAGR, clamped to [−2%, 12%]
       EV     = Σ FCFₙ/(1+r)ⁿ + [FCF_N·(1+g_term)/(r−g_term)]/(1+r)ᴺ
       equity = EV − net debt   fair/share = equity/shares   upside = fair/price − 1
```

The `fcff` method uses the income statement (interest expense, effective tax rate) plus
market weights to compute a real per-company WACC, so high-beta names get a higher discount
rate (e.g. NVDA ~15% vs a low-beta defensive ~5%).

**Caveats — illustrative, not investment advice.** Even `fcff` leans on OCF+after-tax-interest
as the FCFF base (rather than EBIT·(1−t)+D&A−CapEx−ΔNWC), a single historical-CAGR growth
assumption, and book debt as a market-value proxy; it drops firms with non-positive FCF
(e.g. banks). Low-beta names get low WACCs and correspondingly high terminal multiples — a
real DCF sensitivity to be read with care. The point is the **pipeline** — real fundamentals
through a transparent multi-hop DAG in Memcove — not the price targets.

Both workloads are **tooling only** (no `src/memcove` behavior change) beyond the `bench`
optional extra and the two console entry points.

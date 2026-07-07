# Memcove finance benchmark

A **deterministic, model-free** workload that stresses Memcove the way an analytics agent
would: ingest real market data, then build a **multi-hop DAG** of derived datasets —
joins, window functions, aggregates — and time every step. There is no LLM; a fixed plan
stands in for the model, so results are reproducible and isolate the compute/storage
engine (Trino + Iceberg + the registry) from the MCP transport.

## Run

```bash
docker compose up -d --wait          # Memcove stack (Trino, Iceberg, MinIO, Postgres)
uv sync --extra bench                # yfinance + pandas
uv run python benchmarks/finance_benchmark.py --years 8 --replicate 4

# bigger, with the heavy O(n²) correlation step:
uv run python benchmarks/finance_benchmark.py --years 10 --replicate 10 --heavy-corr

# offline (deterministic geometric-Brownian-motion data, no network):
uv run python benchmarks/finance_benchmark.py --synthetic
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--years` | 8 | history depth (rows scale linearly) |
| `--replicate` | 4 | clone each ticker N× with a perturbed price path — real seed data, synthetic scale to grow the universe and join cardinality |
| `--heavy-corr` | off | add the within-sector return-correlation self-join (the heaviest hop) |
| `--synthetic` | off | skip yfinance; use GBM data |
| `--tickers` | — | comma list to override the ~44-name universe |
| `--tenant` | `bench` | tenant namespace to build into (reset at start) |

Real prices are cached under `benchmarks/.cache/`; per-run metrics land in
`benchmarks/results/*.json` (both gitignored).

## The workload

**Ingest** — `prices` (long OHLCV, via the presigned-upload path) + `securities`
(ticker → sector, inline).

**Derive (multi-hop DAG)** — each step is a `derive_dataset` (CTAS) over prior datasets:

| # | Dataset | Shape exercised |
| --- | --- | --- |
| H1 | `daily_returns` | `LAG` window per ticker |
| H2 | `rolling_vol` | 21-day `STDDEV` rolling window |
| H3 | `rolling_ma` | 20/50/200-day `AVG` windows |
| H4 | `returns_by_sector` | join `securities` + `GROUP BY` |
| H5 | `sector_vol_monthly` | join + `date_trunc` + aggregate |
| H6 | `monthly_perf` | `MIN_BY`/`MAX_BY` aggregates |
| H7 | `top_movers` | `RANK` window |
| H8 | `pairwise_corr` | self-join of returns within a sector + `corr()` *(--heavy-corr)* |
| H9 | `signal` | **5-way join** (trend ⋈ risk ⋈ momentum) — deepest lineage |

`signal` depends on `rolling_ma`, `rolling_vol`, `prices`, `securities`, and
`returns_by_sector` — which themselves trace back to `prices`, so it's a genuine
multi-hop derivation with real lineage (inspect it with `inspect_dataset`).

**Query + export** — analytical previews (leaderboard, sector scores, top movers) and a
parquet export of `signal`.

## Sample result

Real yfinance data, 8 years, `--replicate 4 --heavy-corr` (local Docker stack):

```
tickers: 220   price rows: 432,580 (28 MB)   rows materialized: 1.77M   wall: ~15s
ingest prices     432,580 rows   ~395k rows/s
signal (5-way join) 432,150 rows   ~1.7s
pairwise_corr        106 corrs    ~1.4s
```

Numbers are engine-and-hardware dependent — use them for relative comparison across
changes, not as absolute throughput claims.

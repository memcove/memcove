"""Memcove finance benchmark — a deterministic, model-free agent workload.

We *predict we have a model*: instead of an LLM, a fixed plan drives the real Memcove
tools exactly as an agent would — ingest market data, then build a multi-hop DAG of
derived datasets (returns → rolling risk → sector rollups → a composite signal) with
joins, window functions, and aggregates. Every step is timed, so the output is a
benchmark of Memcove's compute/storage engine (Trino + Iceberg + the registry), not the
MCP transport.

Data is real daily OHLCV from yfinance (cached to parquet); a deterministic
geometric-Brownian-motion fallback keeps the benchmark runnable offline. Scale it with
`--years`, `--replicate` (synthesize N perturbed copies of each ticker to grow the
universe), and `--heavy-corr`.

Run (stack must be up: `docker compose up -d --wait`):

    uv sync --extra bench
    uv run memcove-bench --years 8 --replicate 4

    # bigger:
    uv run memcove-bench --years 10 --replicate 10 --heavy-corr
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from memcove.core import storage, trino_client
from memcove.core.config import get_settings
from memcove.core.tenancy import normalize_tenant
from memcove.tools import artifacts, derive, ingest, query

# --- the investable universe: real tickers -> (name, GICS sector) --------------------
UNIVERSE: dict[str, tuple[str, str]] = {
    "AAPL": ("Apple", "Technology"), "MSFT": ("Microsoft", "Technology"),
    "NVDA": ("Nvidia", "Technology"), "AVGO": ("Broadcom", "Technology"),
    "ORCL": ("Oracle", "Technology"), "CRM": ("Salesforce", "Technology"),
    "ADBE": ("Adobe", "Technology"), "CSCO": ("Cisco", "Technology"),
    "GOOGL": ("Alphabet", "Communication"), "META": ("Meta", "Communication"),
    "NFLX": ("Netflix", "Communication"), "DIS": ("Disney", "Communication"),
    "VZ": ("Verizon", "Communication"), "TMUS": ("T-Mobile", "Communication"),
    "AMZN": ("Amazon", "ConsumerDisc"), "TSLA": ("Tesla", "ConsumerDisc"),
    "HD": ("Home Depot", "ConsumerDisc"), "MCD": ("McDonalds", "ConsumerDisc"),
    "NKE": ("Nike", "ConsumerDisc"), "SBUX": ("Starbucks", "ConsumerDisc"),
    "PG": ("P&G", "Staples"), "KO": ("Coca-Cola", "Staples"),
    "PEP": ("Pepsi", "Staples"), "COST": ("Costco", "Staples"),
    "WMT": ("Walmart", "Staples"), "JPM": ("JPMorgan", "Financials"),
    "BAC": ("Bank of America", "Financials"), "WFC": ("Wells Fargo", "Financials"),
    "GS": ("Goldman Sachs", "Financials"), "MS": ("Morgan Stanley", "Financials"),
    "V": ("Visa", "Financials"), "MA": ("Mastercard", "Financials"),
    "UNH": ("UnitedHealth", "HealthCare"), "JNJ": ("J&J", "HealthCare"),
    "LLY": ("Eli Lilly", "HealthCare"), "PFE": ("Pfizer", "HealthCare"),
    "ABBV": ("AbbVie", "HealthCare"), "MRK": ("Merck", "HealthCare"),
    "XOM": ("Exxon", "Energy"), "CVX": ("Chevron", "Energy"),
    "BA": ("Boeing", "Industrials"), "CAT": ("Caterpillar", "Industrials"),
    "GE": ("GE", "Industrials"), "HON": ("Honeywell", "Industrials"),
}

# --- the multi-hop derivation DAG: (label, description, mode-of-thought, sql) ---------
# Each SELECT references prior datasets by bare name; the SQL guard qualifies them to the
# caller's namespace. Deep lineage: signal <- {rolling_ma, rolling_vol, prices,
# securities, returns_by_sector} <- {daily_returns, prices, securities} <- prices.
DERIVATIONS: list[tuple[str, str, str]] = [
    (
        "daily_returns",
        "H1 close-to-close returns (LAG window per ticker)",
        """
        SELECT dt, ticker, close,
               close / lag(close) OVER (PARTITION BY ticker ORDER BY dt) - 1 AS ret
        FROM prices
        """,
    ),
    (
        "rolling_vol",
        "H2 21-day rolling volatility (STDDEV window)",
        """
        SELECT dt, ticker, ret,
               stddev(ret) OVER (
                   PARTITION BY ticker ORDER BY dt ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
               ) AS vol_21
        FROM daily_returns
        WHERE ret IS NOT NULL
        """,
    ),
    (
        "rolling_ma",
        "H3 20/50/200-day moving averages (AVG windows)",
        """
        SELECT dt, ticker, close,
               avg(close) OVER (PARTITION BY ticker ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
               avg(close) OVER (PARTITION BY ticker ORDER BY dt ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS ma50,
               avg(close) OVER (PARTITION BY ticker ORDER BY dt ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS ma200
        FROM prices
        """,
    ),
    (
        "returns_by_sector",
        "H4 avg daily return per sector (JOIN securities + GROUP BY)",
        """
        SELECT r.dt, s.sector, avg(r.ret) AS avg_ret, count(*) AS n
        FROM daily_returns r JOIN securities s ON r.ticker = s.ticker
        WHERE r.ret IS NOT NULL
        GROUP BY r.dt, s.sector
        """,
    ),
    (
        "sector_vol_monthly",
        "H5 monthly avg volatility per sector (JOIN + date_trunc + GROUP BY)",
        """
        SELECT date_trunc('month', v.dt) AS month, s.sector, avg(v.vol_21) AS avg_vol
        FROM rolling_vol v JOIN securities s ON v.ticker = s.ticker
        WHERE v.vol_21 IS NOT NULL
        GROUP BY date_trunc('month', v.dt), s.sector
        """,
    ),
    (
        "monthly_perf",
        "H6 monthly return per ticker (MIN_BY/MAX_BY aggregates)",
        """
        SELECT ticker, date_trunc('month', dt) AS month,
               max_by(close, dt) / min_by(close, dt) - 1 AS monthly_ret
        FROM prices
        GROUP BY ticker, date_trunc('month', dt)
        """,
    ),
    (
        "top_movers",
        "H7 top-10 monthly movers (RANK window)",
        """
        SELECT month, ticker, monthly_ret, rnk FROM (
            SELECT month, ticker, monthly_ret,
                   rank() OVER (PARTITION BY month ORDER BY monthly_ret DESC) AS rnk
            FROM monthly_perf
        ) WHERE rnk <= 10
        """,
    ),
    (
        "signal",
        "H9 composite trend/risk/momentum signal (5-way JOIN, deep lineage)",
        """
        SELECT p.dt, p.ticker, s.sector,
               m.close / m.ma50 - 1 AS trend,
               v.vol_21 AS risk,
               sec.avg_ret AS sector_mom,
               (m.close / m.ma50 - 1) / nullif(v.vol_21, 0) AS score
        FROM rolling_ma m
        JOIN rolling_vol v ON v.ticker = m.ticker AND v.dt = m.dt
        JOIN prices p ON p.ticker = m.ticker AND p.dt = m.dt
        JOIN securities s ON s.ticker = m.ticker
        JOIN returns_by_sector sec ON sec.sector = s.sector AND sec.dt = m.dt
        WHERE m.ma50 IS NOT NULL AND v.vol_21 IS NOT NULL
        """,
    ),
]

# H8 pairwise correlation — the heaviest step (self-join of returns within a sector).
# Restricted to base (non-replicated) tickers so it stays bounded as --replicate grows.
CORR_SQL = """
    SELECT a.ticker AS t1, b.ticker AS t2, s.sector,
           corr(a.ret, b.ret) AS correlation, count(*) AS n
    FROM daily_returns a
    JOIN daily_returns b ON a.dt = b.dt AND a.ticker < b.ticker
    JOIN securities s ON a.ticker = s.ticker
    JOIN securities s2 ON b.ticker = s2.ticker AND s2.sector = s.sector
    WHERE a.ret IS NOT NULL AND b.ret IS NOT NULL
      AND a.ticker NOT LIKE '%#%' AND b.ticker NOT LIKE '%#%'
    GROUP BY a.ticker, b.ticker, s.sector
    HAVING count(*) > 60
"""

# Analytical queries run at the end (measured previews).
QUERIES: list[tuple[str, str]] = [
    ("latest_leaderboard",
     "SELECT ticker, sector, score FROM signal WHERE dt = (SELECT max(dt) FROM signal) "
     "ORDER BY score DESC LIMIT 20"),
    ("sector_scores",
     "SELECT sector, avg(score) AS avg_score, count(*) AS n FROM signal "
     "GROUP BY sector ORDER BY avg_score DESC"),
    ("recent_top_movers",
     "SELECT ticker, monthly_ret FROM top_movers WHERE month = (SELECT max(month) FROM top_movers) "
     "AND rnk <= 10 ORDER BY monthly_ret DESC"),
]


@dataclass
class Phase:
    name: str
    kind: str  # ingest | derive | query | export
    rows: int
    seconds: float
    detail: str = ""


@dataclass
class Bench:
    phases: list[Phase] = field(default_factory=list)

    def record(self, name, kind, rows, seconds, detail=""):
        self.phases.append(Phase(name, kind, rows, seconds, detail))
        rps = f"{rows / seconds:,.0f}/s" if seconds > 0 and rows else "-"
        print(f"  [{kind:<6}] {name:<22} {rows:>12,} rows  {seconds:>7.2f}s  {rps:>12}")


# ------------------------------------------------------------------ data loading

def _cache_path(out_dir: Path, key: str) -> Path:
    d = out_dir / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.parquet"


def fetch_prices(tickers: list[str], years: int, synthetic: bool, out_dir: Path) -> "pa.Table":
    """Real daily OHLCV from yfinance (cached), or a deterministic GBM fallback."""
    cache = _cache_path(out_dir, f"prices_{years}y_{len(tickers)}")
    if cache.exists():
        print(f"  (loaded cached prices: {cache.name})")
        return pq.read_table(cache)

    if not synthetic:
        try:
            table = _fetch_yfinance(tickers, years)
            pq.write_table(table, cache)
            return table
        except Exception as exc:  # noqa: BLE001
            print(f"  ! yfinance fetch failed ({exc}); falling back to synthetic data")

    table = _synthetic_prices(tickers, years)
    pq.write_table(table, cache)
    return table


def _fetch_yfinance(tickers: list[str], years: int) -> "pa.Table":
    import datetime as _dt

    import pandas as pd
    import yfinance as yf

    end = _dt.date.today()
    start = end - _dt.timedelta(days=int(years * 365.25) + 5)
    print(f"  fetching {len(tickers)} tickers x {years}y from yfinance ...")
    raw = yf.download(
        tickers, start=start.isoformat(), end=end.isoformat(),
        auto_adjust=False, progress=False, group_by="ticker", threads=True,
    )
    if raw is None or raw.empty:
        raise RuntimeError("empty yfinance response")
    frames = []
    for t in tickers:
        if t not in raw.columns.get_level_values(0):
            continue
        sub = raw[t].reset_index()
        sub.columns = [str(c).lower().replace(" ", "_") for c in sub.columns]
        sub = sub.rename(columns={"date": "dt"})
        sub["ticker"] = t
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True).dropna(subset=["close"])
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    cols = ["dt", "ticker", "open", "high", "low", "close", "adj_close", "volume"]
    df = df[[c for c in cols if c in df.columns]]
    return pa.Table.from_pandas(df, preserve_index=False)


def _synthetic_prices(tickers: list[str], years: int) -> "pa.Table":
    """Deterministic geometric-Brownian-motion OHLCV (offline fallback)."""
    import datetime as _dt

    import numpy as np

    end = _dt.date.today()
    n_days = int(years * 252)
    # business days back from today
    dates = [end - _dt.timedelta(days=i) for i in range(int(n_days * 1.5))]
    dates = [d for d in dates if d.weekday() < 5][:n_days][::-1]
    print(f"  synthesizing {len(tickers)} tickers x {len(dates)} business days (GBM)")

    rows: list[dict] = []
    for t in tickers:
        rng = np.random.default_rng(abs(hash(t)) % (2**32))
        mu, sig = 0.08 / 252, 0.02
        rets = rng.normal(mu, sig, len(dates))
        prev = 50.0 + (abs(hash(t)) % 400)
        for i, d in enumerate(dates):
            close = max(1.0, prev * (1 + rets[i]))
            op = prev
            hi = max(op, close) * (1 + abs(rng.normal(0, 0.004)))
            lo = min(op, close) * (1 - abs(rng.normal(0, 0.004)))
            rows.append({
                "dt": d, "ticker": t, "open": round(op, 2), "high": round(hi, 2),
                "low": round(lo, 2), "close": round(close, 2), "adj_close": round(close, 2),
                "volume": int(rng.integers(5e5, 5e7)),
            })
            prev = close
    dt_col = [r["dt"] for r in rows]
    return pa.table({
        "dt": pa.array(dt_col, type=pa.date32()),
        "ticker": [r["ticker"] for r in rows],
        "open": [r["open"] for r in rows], "high": [r["high"] for r in rows],
        "low": [r["low"] for r in rows], "close": [r["close"] for r in rows],
        "adj_close": [r["adj_close"] for r in rows], "volume": [r["volume"] for r in rows],
    })


def replicate_universe(prices: "pa.Table", factor: int) -> tuple["pa.Table", dict]:
    """Grow the universe by cloning each ticker `factor` times with a perturbed price
    path (T#1, T#2, ...). Real seed data, synthetic scale — stresses joins/aggregates."""
    securities = {t: meta for t, meta in UNIVERSE.items()}
    if factor <= 0:
        return prices, securities

    import numpy as np

    df = prices.to_pandas()
    clones = [df]
    for k in range(1, factor + 1):
        rng = np.random.default_rng(1000 + k)
        c = df.copy()
        scale = rng.uniform(0.7, 1.4, len(c))
        for col in ("open", "high", "low", "close", "adj_close"):
            c[col] = (c[col] * scale).round(2)
        c["ticker"] = c["ticker"] + f"#{k}"
        clones.append(c)
        for base, (name, sector) in UNIVERSE.items():
            securities[f"{base}#{k}"] = (f"{name} clone {k}", sector)
    import pandas as pd
    return pa.Table.from_pandas(pd.concat(clones, ignore_index=True), preserve_index=False), securities


# ------------------------------------------------------------------ memcove ops

def reset_tenant(tenant: str) -> None:
    from memcove.core import catalog, registry
    for label in catalog.list_labels(tenant):
        try:
            catalog.drop_table(tenant, label)
        except Exception:  # noqa: BLE001
            pass
    for label in registry.labels_for_tenant(tenant):
        registry.delete_object(tenant, label)


def ingest_big(tenant: str, label: str, table: "pa.Table") -> None:
    """Ingest a large table via the upload path: stage parquet, then remember by handle."""
    tk = ingest.request_upload(tenant, label)
    bucket = storage.resolve(get_settings().staging_bucket)[0]
    storage.write_parquet_table(table, bucket, tk.upload_handle)
    ingest.ingest_object(
        tenant, label, {"kind": "upload_handle", "handle": tk.upload_handle}, mode="replace",
    )


def count_rows(tenant: str, label: str) -> int:
    s = get_settings()
    return int(trino_client.scalar(f'SELECT count(*) FROM "{s.trino_catalog}"."{tenant}"."{label}"'))


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description="Memcove finance benchmark")
    ap.add_argument("--years", type=int, default=8)
    ap.add_argument("--replicate", type=int, default=4, help="synthetic clones per ticker")
    ap.add_argument("--tenant", default="bench")
    ap.add_argument("--synthetic", action="store_true", help="skip yfinance, use GBM data")
    ap.add_argument("--heavy-corr", action="store_true", help="run the O(n^2) correlation step")
    ap.add_argument("--tickers", default="", help="comma list to override the universe")
    ap.add_argument("--out-dir", default="benchmark-output",
                    help="where price cache + result JSON are written (default: ./benchmark-output)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    tenant = normalize_tenant(args.tenant)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or list(UNIVERSE)
    bench = Bench()

    print(f"\n== Memcove finance benchmark ==  tenant={tenant}  "
          f"tickers={len(tickers)}  years={args.years}  replicate={args.replicate}\n")

    # 1. Load market data (real or synthetic) and scale the universe.
    prices = fetch_prices(tickers, args.years, args.synthetic, out_dir)
    prices, securities = replicate_universe(prices, args.replicate)
    n_tickers = len({*securities})
    print(f"  dataset: {prices.num_rows:,} price rows across {n_tickers} tickers, "
          f"{prices.nbytes / 1e6:.1f} MB in memory\n")

    # 2. Ingest into Memcove.
    print("-- ingest --")
    from memcove.core import registry
    registry.init_db()
    reset_tenant(tenant)

    t0 = time.perf_counter()
    ingest_big(tenant, "prices", prices)
    bench.record("prices", "ingest", prices.num_rows, time.perf_counter() - t0)

    sec_records = [{"ticker": t, "name": n, "sector": s} for t, (n, s) in securities.items()]
    t0 = time.perf_counter()
    ingest.ingest_object(
        tenant, "securities",
        {"kind": "inline", "format": "json_records", "records": sec_records}, mode="replace",
    )
    bench.record("securities", "ingest", len(sec_records), time.perf_counter() - t0)

    # 3. Multi-hop derivations.
    print("\n-- derive (multi-hop DAG) --")
    derivations = list(DERIVATIONS)
    if args.heavy_corr:
        derivations.insert(7, ("pairwise_corr", "H8 within-sector return correlations (self-join)", CORR_SQL))
    for label, desc, sql in derivations:
        t0 = time.perf_counter()
        derive.derive_object(tenant, label, sql, mode="replace")
        dur = time.perf_counter() - t0
        bench.record(label, "derive", count_rows(tenant, label), dur, desc)

    # 4. Analytical queries.
    print("\n-- query --")
    for name, sql in QUERIES:
        t0 = time.perf_counter()
        res = query.run_query(tenant, sql, limit=50)
        bench.record(name, "query", res.row_count, time.perf_counter() - t0)

    # 5. Export the signal table as an artifact (dogfood the export path).
    print("\n-- export --")
    t0 = time.perf_counter()
    ref = artifacts.export_artifact(tenant, fmt="parquet", label="signal")
    bench.record("signal.parquet", "export", ref.row_count, time.perf_counter() - t0,
                 f"{ref.size_bytes / 1e6:.1f} MB -> {ref.uri}")

    # 6. Summary.
    _summary(bench, tenant, n_tickers, args, out_dir)


def _summary(bench: Bench, tenant: str, n_tickers: int, args, out_dir: Path) -> None:
    total = sum(p.seconds for p in bench.phases)
    ingest_rows = sum(p.rows for p in bench.phases if p.kind == "ingest")
    derive_rows = sum(p.rows for p in bench.phases if p.kind == "derive")
    print("\n== summary ==")
    print(f"  phases          : {len(bench.phases)}")
    print(f"  tickers         : {n_tickers}")
    print(f"  rows ingested   : {ingest_rows:,}")
    print(f"  rows materialized (derive) : {derive_rows:,}")
    print(f"  wall time       : {total:.1f}s")
    slowest = sorted(bench.phases, key=lambda p: p.seconds, reverse=True)[:3]
    print("  slowest phases  : " + ", ".join(f"{p.name} ({p.seconds:.1f}s)" for p in slowest))

    out = out_dir / "results"
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "tenant": tenant, "tickers": n_tickers, "years": args.years,
        "replicate": args.replicate, "heavy_corr": args.heavy_corr,
        "total_seconds": round(total, 3),
        "phases": [vars(p) for p in bench.phases],
    }
    path = out / f"bench_{n_tickers}t_{args.years}y.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    print(f"  results written : {path}")


if __name__ == "__main__":
    main()

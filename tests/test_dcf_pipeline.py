"""Unit tests for the DCF pipeline's data-loading layer (no stack required).

The DCF math itself lives in Trino SQL and is exercised by the live pipeline run; here we
cover the pure-Python surface: the synthetic fallback and the statement-row lookup helper.
"""

from __future__ import annotations

import pandas as pd

from memcove.benchmarks import dcf


def test_synthetic_data_is_well_formed():
    fund, market = dcf._synthetic(["AAA", "BBB"])

    # every ticker has a multi-year history and a market snapshot
    assert {r["ticker"] for r in market} == {"AAA", "BBB"}
    assert {r["ticker"] for r in fund} == {"AAA", "BBB"}
    assert len(fund) == len(market) * 4  # 4 fiscal years each

    f = fund[0]
    assert set(f) == {"ticker", "fiscal_year", "ocf", "capex", "interest", "tax_rate"}
    assert f["ocf"] > 0 and f["capex"] < 0  # capex reported negative, so FCF = ocf + capex
    assert f["interest"] >= 0 and 0 < f["tax_rate"] < 1  # WACC inputs for the fcff method

    m = market[0]
    assert set(m) == {"ticker", "price", "shares", "beta", "total_debt", "cash"}
    assert m["price"] > 0 and m["shares"] > 0 and m["beta"] > 0


def test_synthetic_is_deterministic():
    # deterministic seeds → identical output across runs (reproducible benchmarks)
    assert dcf._synthetic(["AAA"]) == dcf._synthetic(["AAA"])


def test_row_lookup_matches_first_candidate_and_tolerates_missing():
    df = pd.DataFrame(
        {"2023": [100.0, -30.0]},
        index=["Operating Cash Flow", "Capital Expenditure"],
    )
    # first matching candidate label wins
    assert dcf._row(df, "Total Cash From Operating Activities", "Operating Cash Flow")["2023"] == 100.0
    # a missing row yields an empty Series whose .get(...) is None (skipped by the loader)
    assert dcf._row(df, "Free Cash Flow").get("2023") is None

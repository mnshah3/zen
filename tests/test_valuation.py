"""The month-end P/E history (zen/valuation.py)."""

from __future__ import annotations

import pandas as pd
import pytest

from zen import valuation


def _px(prices):
    d = pd.bdate_range("2025-01-01", periods=len(prices))
    return pd.DataFrame({"date": d, "adj": prices})


def _fil(rows):
    return pd.DataFrame(rows, columns=["period_end", "broadcast_dt", "eps", "profit"])


QUARTERS = [("2024-03-31", "2024-05-10", 5.0, 50.0), ("2024-06-30", "2024-08-10", 5.0, 50.0),
            ("2024-09-30", "2024-11-10", 5.0, 50.0), ("2024-12-31", "2025-02-10", 5.0, 50.0)]


def test_pe_is_price_over_trailing_eps_known_by_each_month_end():
    px = pd.DataFrame({"date": pd.to_datetime(["2025-01-31", "2025-02-28"]), "adj": [100.0, 120.0]})
    s = valuation.pe_series(_fil(QUARTERS), None, px, months=3, asof=pd.Timestamp("2025-02-28"))
    # January: the December quarter was not filed yet, so only three quarters are known
    assert s == [{"d": "2025-01-31", "pe": None}, {"d": "2025-02-28", "pe": 6.0}]


def test_eps_is_restated_for_a_later_split():
    px = pd.DataFrame({"date": pd.to_datetime(["2025-02-28"]), "adj": [60.0]})
    fac = pd.DataFrame({"ex_date": [pd.Timestamp("2025-06-01")], "factor": [0.5]})   # 2-for-1 later
    s = valuation.pe_series(_fil(QUARTERS), fac, px, months=1, asof=pd.Timestamp("2025-02-28"))
    assert s[0]["pe"] == pytest.approx(60.0 / (20.0 * 0.5))


def test_a_revision_known_later_does_not_reach_back():
    rows = QUARTERS + [("2024-12-31", "2025-03-20", 10.0, 100.0)]          # revised in March
    px = pd.DataFrame({"date": pd.to_datetime(["2025-02-28", "2025-03-31"]), "adj": [100.0, 100.0]})
    s = valuation.pe_series(_fil(rows), None, px, months=2, asof=pd.Timestamp("2025-03-31"))
    assert [x["pe"] for x in s] == [5.0, 4.0]


def test_mixed_share_bases_a_loss_and_a_price_break_leave_gaps():
    px = pd.DataFrame({"date": pd.to_datetime(["2025-02-28"]), "adj": [100.0]})
    mixed = [list(q) for q in QUARTERS]
    mixed[1][2] = 50.0                                                     # EPS on a tenth of the shares
    assert valuation.pe_series(_fil(mixed), None, px, months=1, asof=pd.Timestamp("2025-02-28"))[0]["pe"] is None
    loss = [(a, b, -1.0, -10.0) for a, b, _, _ in QUARTERS]
    assert valuation.pe_series(_fil(loss), None, px, months=1, asof=pd.Timestamp("2025-02-28"))[0]["pe"] is None
    s = valuation.pe_series(_fil(QUARTERS), None, px, brk=[pd.Timestamp("2024-12-15")], months=1,
                            asof=pd.Timestamp("2025-02-28"))
    assert s[0]["pe"] is None


def test_summary_and_breaks():
    series = [{"d": f"2025-{m:02d}-28", "pe": float(m)} for m in range(1, 13)]
    sm = valuation.summary(series)
    assert sm["median"] == 6.5 and sm["low"] == 1.0 and sm["high"] == 12.0 and sm["latest"] == 12.0
    assert sm["pct_at_or_below"] == 1.0 and valuation.summary(series[:5]) is None
    px = pd.DataFrame({"date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]), "adj": [100.0, 101.0, 200.0]})
    assert valuation.price_breaks(px) == [pd.Timestamp("2025-01-03")]

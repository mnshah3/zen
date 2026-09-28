"""Synthetic tests of the v2 independent checker (jobs/crosscheck_v2.py).

No archive is read. Each test builds a small frame or panel by hand and checks
one v2 rule against a figure worked out independently here: the 1/sigma sizing
with its bounds, the graded trend filter, A1's two indicators, EBIT, the
balance-sheet choice of the Clarification to A5, and the tranche book (both
entry variants, cancellation at the next decision date and on a forced exit,
and A1's adoption metric).
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from jobs import crosscheck_v2 as cc


# ------------------------------------------------------------------ sizing
def test_equal_sigmas_give_equal_weights_summing_to_the_invested_share():
    w, bound, info = cc.size_weights([0.02] * 12, 0.20)
    assert np.allclose(w, 0.8 / 12)
    assert math.isclose(w.sum(), 0.8)
    assert (bound == "").all() and info["left_as_cash"] == pytest.approx(0.0, abs=1e-15)


def test_one_quiet_name_is_capped_and_the_excess_spread_pro_rata():
    # raw 1/sigma: 100 and eleven 50s -> 100/650 = 0.1538 > 1.5/12 = 0.125
    w, bound, _ = cc.size_weights([0.01] + [0.02] * 11, 0.0)
    assert w[0] == pytest.approx(0.125)
    assert bound[0] == "upper"
    assert np.allclose(w[1:], (1 - 0.125) / 11)
    assert w.sum() == pytest.approx(1.0)


def test_a_noisy_name_is_floored_and_the_shortfall_taken_pro_rata():
    sig = [1.0] + [0.02] * 11              # raw 1 vs 50: far below 0.5/12
    w, bound, _ = cc.size_weights(sig, 0.35)
    e = 0.65 / 12
    assert w[0] == pytest.approx(0.5 * e) and bound[0] == "lower"
    assert np.allclose(w[1:], (0.65 - 0.5 * e) / 11)
    assert w.sum() == pytest.approx(0.65)


def test_weights_always_within_bounds_and_never_above_the_invested_share():
    rng = np.random.default_rng(7)
    for _ in range(300):
        n = int(rng.integers(1, 13))
        sig = np.exp(rng.normal(-3.5, 0.8, n))
        cf = float(rng.choice([0.0, 0.2, 0.275, 0.35]))
        w, _, info = cc.size_weights(sig, cf)
        e = (1 - cf) / 12
        assert (w >= 0.5 * e - 1e-15).all() and (w <= 1.5 * e + 1e-15).all()
        assert w.sum() <= (1 - cf) + 1e-12
        if n == 12:
            assert w.sum() == pytest.approx(1 - cf)


def test_fewer_than_twelve_names_leave_the_capped_remainder_as_cash():
    w, bound, info = cc.size_weights([0.02] * 5, 0.0)
    assert np.allclose(w, 1.5 / 12) and (bound == "upper").all()
    assert info["left_as_cash"] == pytest.approx(1 - 5 * 1.5 / 12)


def test_a_name_with_no_sigma_starts_at_the_equal_weight_and_the_others_scale_around_it():
    # Clarification after the in-sample engine comparison. e = 0.8/12. Before
    # the bounds: the no-sigma name e; the other eleven share 11e by 1/sigma
    # (100 and ten 50s): 11e/6 and 11e/12 each. 11e/6 > 1.5e: capped, and the
    # excess e/3 goes pro rata over the free names (sum 122e/12), the
    # no-sigma name included.
    w, bound, info = cc.size_weights([np.nan, 0.01] + [0.02] * 10, 0.20)
    e = 0.8 / 12
    assert info["no_sigma"] == 1
    assert w[0] == pytest.approx(e * (1 + 2 / 61), rel=1e-12)
    assert w[1] == pytest.approx(1.5 * e) and bound[1] == "upper"
    assert np.allclose(w[2:], 11 * e / 12 * 126 / 122, rtol=1e-12)
    assert w.sum() == pytest.approx(0.8, rel=1e-12)
    # every sigma missing: equal weights
    w, _, _ = cc.size_weights([np.nan] * 12, 0.0)
    assert np.allclose(w, 1 / 12)


# ------------------------------------------------------------------ trend filter
def _tri(values, start="2018-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(np.asarray(values, float), index=idx)


def test_a_rising_market_holds_no_cash():
    s = _tri(np.linspace(100, 200, 400))
    D = s.index[-1] + pd.Timedelta(days=1)
    tr = cc.trend_at(s, D, s.index[-1])
    assert (tr["c1"], tr["c2"], tr["c3"], tr["cash_fraction"]) == (False, False, False, 0.0)


def test_a_long_deep_fall_meets_all_three_conditions():
    s = _tri(np.r_[np.linspace(100, 200, 300), np.linspace(200, 120, 200)])
    D = s.index[-1] + pd.Timedelta(days=1)
    tr = cc.trend_at(s, D, s.index[-1])
    assert (tr["c1"], tr["c2"], tr["c3"]) == (True, True, True)
    assert tr["cash_fraction"] == 0.35


def test_a_shallow_fresh_dip_meets_only_the_first():
    x = np.r_[np.full(399, 100.0), 99.0]         # 1% below a flat 200-day mean, just now
    s = _tri(x)
    D = s.index[-1] + pd.Timedelta(days=1)
    tr = cc.trend_at(s, D, s.index[-1])
    assert (tr["c1"], tr["c2"], tr["c3"]) == (True, False, False)
    assert tr["cash_fraction"] == 0.20
    assert tr["tri_below_sma_sessions_of_126"] == 1


def test_the_persistence_count_uses_each_sessions_own_trailing_mean():
    # 76 of the last 126 closes below their own 200-close mean -> condition 3 (>= 75.6)
    base = np.full(400, 100.0)
    base[-76:] = 99.9                             # each is below its trailing mean (which includes 100s)
    tr = cc.trend_at(_tri(base), _tri(base).index[-1] + pd.Timedelta(days=1), _tri(base).index[-1])
    assert tr["tri_below_sma_sessions_of_126"] == 76 and tr["c3"]
    base[-76] = 100.0                             # 75 below: not enough
    tr = cc.trend_at(_tri(base), _tri(base).index[-1] + pd.Timedelta(days=1), _tri(base).index[-1])
    assert tr["tri_below_sma_sessions_of_126"] == 75 and not tr["c3"]


def test_the_trend_filter_refuses_when_the_previous_session_is_missing():
    s = _tri(np.linspace(100, 200, 400))
    with pytest.raises(SystemExit):
        cc.trend_at(s.iloc[:-1], s.index[-1] + pd.Timedelta(days=1), s.index[-1])


# ------------------------------------------------------------------ A1 indicators
def _wilder_rsi(closes, n=14):
    d = np.diff(closes)
    g, l = np.maximum(d, 0), np.maximum(-d, 0)
    ag, al = g[:n].mean(), l[:n].mean()
    out = [np.nan] * n
    out.append(100 - 100 / (1 + ag / al))
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out.append(100 - 100 / (1 + ag / al))
    return np.array(out)


def test_rsi_and_the_50_session_mean_match_a_plain_loop():
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
    P = SimpleNamespace(adj=c.reshape(-1, 1))
    x, above, rsi, ok = cc.a1_indicators(P)
    ref = _wilder_rsi(c)
    assert np.isnan(rsi[:14, 0]).all()
    assert np.allclose(rsi[14:, 0], ref[14:], rtol=0, atol=1e-10)
    assert np.isnan(above[:49, 0]).all()
    for t in (49, 120, 199):
        assert above[t, 0] == pytest.approx(c[t] / c[t - 49:t + 1].mean() - 1, abs=1e-14)
    want = (c <= 1.1 * np.r_[np.full(49, np.nan), [c[t - 49:t + 1].mean() for t in range(49, 200)]]) \
        & (np.nan_to_num(ref, nan=100) < 70)
    assert (ok[:, 0] == want).all()


def test_a_session_without_a_trade_carries_the_last_close():
    c = np.linspace(100, 160, 80)
    gap = c.copy()
    gap[60] = np.nan                              # no trade on session 60
    P = SimpleNamespace(adj=gap.reshape(-1, 1))
    x, above, rsi, _ = cc.a1_indicators(P)
    assert x[60, 0] == c[59]
    carried = c.copy()
    carried[60] = c[59]
    assert above[70, 0] == pytest.approx(carried[70] / carried[21:71].mean() - 1, abs=1e-14)


def test_a_straight_rise_is_never_bought_under_a1():
    c = np.linspace(100, 300, 120)                # RSI 100 throughout
    _, _, rsi, ok = cc.a1_indicators(SimpleNamespace(adj=c.reshape(-1, 1)))
    assert np.nanmin(rsi[14:, 0]) == 100.0 and not ok.any()


def test_rsi_is_undefined_and_the_condition_fails_when_gain_and_loss_are_both_zero():
    c = np.full(80, 100.0)                        # flat: average gain = average loss = 0
    _, above, rsi, ok = cc.a1_indicators(SimpleNamespace(adj=c.reshape(-1, 1)))
    assert np.isnan(rsi[:, 0]).all()
    assert (above[49:, 0] == 0.0).all()           # the 50-session test alone would pass
    assert not ok.any()


def _pre_panel(pre_rows, pre_sessions):
    fr = pd.DataFrame(pre_rows, columns=["date", "cid", "close"])
    fr["date"] = pd.to_datetime(fr["date"])
    return cc.PreCloses(fr, pd.DatetimeIndex(pre_sessions))


def test_rsi_starts_at_the_first_close_in_the_archive_not_at_the_panel_start():
    # Clarification after the in-sample engine comparison: the RSI starts at
    # the stock's first EQ/BE close in the archive; closes before the panel
    # are read for it, carried over a session without a trade.
    rng = np.random.default_rng(11)
    pre_s = pd.bdate_range(end="2020-12-31", periods=60)
    first = 5                                     # A's first close in the archive
    pre_c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 60)))
    pre_rows = [(d, "A", pre_c[i]) for i, d in enumerate(pre_s) if i >= first and i != 30]   # no trade at 30
    pan_c = pre_c[-1] * np.exp(np.cumsum(rng.normal(0, 0.02, 130)))
    P = _panel(prices={"A": pan_c, "B": np.full(130, 50.0)})
    x, above, rsi, ok = cc.a1_indicators(P, _pre_panel(pre_rows, pre_s))
    full = np.r_[pre_c[first:], pan_c]
    full[30 - first] = full[29 - first]           # carried close
    ref = _wilder_rsi(full)[-130:]
    assert np.allclose(rsi[:, 0], ref, rtol=0, atol=1e-10)
    assert np.allclose(x[:, 0], pan_c)
    # the 50-session mean reaches back into the closes before the panel
    assert above[5, 0] == pytest.approx(full[-130 + 5] / full[-130 + 5 - 49:-130 + 6].mean() - 1, abs=1e-14)
    # seeding at the panel start instead gives a different RSI early in the panel
    _, _, rsi_panel_only, _ = cc.a1_indicators(P)
    assert abs(rsi_panel_only[14, 0] - rsi[14, 0]) > 1e-6


def test_closes_before_the_panel_are_adjusted_into_the_panels_basis():
    # A: 400 before a 2:1 split on a pre-panel session, 200 after it, 100 after a
    # 1:1 bonus whose ex-date (a Saturday) falls between the last pre-panel
    # session and the panel's first, 50 after a split inside the panel. Every
    # adjusted close is 50, so the gain and loss averages are both zero.
    pre_s = pd.bdate_range(end="2020-12-31", periods=40)
    sessions = pd.bdate_range("2021-01-04", periods=80)
    pre_rows = [(d, "A", 400.0 if i < 20 else 200.0) for i, d in enumerate(pre_s)]
    rows = [(d, "A", "EQ", ISINS["A"], 100.0 if t < 50 else 50.0, 100.0 if t < 50 else 50.0, 1e7)
            for t, d in enumerate(sessions)]
    p = pd.DataFrame(rows, columns=["date", "symbol", "series", "isin", "open", "close", "turnover"])
    spells = pd.DataFrame({"symbol": ["A"], "isin": [ISINS["A"]], "cid": ["A"]})
    ca = pd.DataFrame({"cid": ["A", "A", "A"],
                       "ex_date": pd.to_datetime([pre_s[20], "2021-01-02", sessions[50]]),
                       "factor": [0.5, 0.5, 0.5]})
    P = cc.Panels(p, spells, sessions, ca)
    xp = cc.pre_adjusted_closes(P, _pre_panel(pre_rows, pre_s))
    assert np.allclose(xp[:, 0], 50.0) and np.allclose(P.adj[:, 0], 50.0)
    _, _, rsi, ok = cc.a1_indicators(P, _pre_panel(pre_rows, pre_s))
    assert np.isnan(rsi).all() and not ok.any()


# ------------------------------------------------------------------ EBIT and the balance sheet
def test_ebit_is_pre_exceptional_profit_plus_finance_costs_with_the_stand_in():
    g = pd.DataFrame({"pbt_before_exceptional": [10.0, np.nan, np.nan, 5.0],
                      "finance_costs": [2.0, 1.0, 1.0, np.nan],
                      "pbt": [9.0, 7.0, np.nan, 5.0],
                      "exceptional_items": [-1.0, np.nan, np.nan, np.nan]})
    e = cc.quarter_ebit(g)
    assert e.iloc[0] == 12.0                      # other income stays in, exceptional stays out
    assert e.iloc[1] == 8.0                       # pbt - 0 exceptional + finance costs
    assert np.isnan(e.iloc[2])                    # no profit line at all
    # Clarification after the in-sample engine comparison: a quarter with no
    # finance-costs line has no EBIT (missing, not 0)
    assert np.isnan(e.iloc[3])


def _bs(rows):
    cols = ["cid", "consolidated", "period_end", "broadcast_dt", "xbrl_url", "has_bs", "has_is",
            "equity", "equity_capital", "assets", "debt_long", "debt_short"]
    f = pd.DataFrame(rows, columns=cols)
    f["period_end"] = pd.to_datetime(f["period_end"])
    f["broadcast_dt"] = pd.to_datetime(f["broadcast_dt"])
    f["qn"] = f["period_end"].dt.year * 12 + f["period_end"].dt.month
    return f.sort_values(["broadcast_dt", "xbrl_url"])


def test_latest_period_then_latest_revision_including_a_filing_without_income_statement():
    f = _bs([
        ("A", False, "2023-03-31", "2023-05-20", "a1", True, True, 100, 10, 300, 20, 10),
        ("A", False, "2023-09-30", "2023-11-10", "a2", True, True, 110, 10, 320, 30, 5),
        ("A", False, "2023-09-30", "2023-12-01", "a3", True, False, 120, 10, 330, 40, np.nan),  # revision, no IS
        ("A", False, "2023-12-31", "2024-02-10", "a4", False, True, np.nan, np.nan, np.nan, np.nan, np.nan),
    ])
    out = cc.balance_sheets_at(f, pd.Timestamp("2024-02-15"), set(), pd.Series({"A": False}))
    r = out.loc["A"]
    assert r["bs_url"] == "a3" and r["equity"] == 120 and r["debt"] == 40
    assert bool(r["bs_usable"]) and r["bs_reason"] == "ok"


def test_the_basis_filings_set_aside_age_and_equity_rules():
    f = _bs([
        # consolidated company: a standalone balance sheet is not in its basis
        ("B", True, "2023-03-31", "2023-05-20", "b1", True, True, 100, 10, 300, np.nan, np.nan),
        ("B", False, "2023-09-30", "2023-11-10", "b2", True, True, 90, 10, 280, 5, 5),
        # set aside by the income scale screen: fall back to the earlier period
        ("C", False, "2023-03-31", "2023-05-20", "c1", True, True, 100, 10, 300, 1, 1),
        ("C", False, "2023-09-30", "2023-11-10", "c2", True, True, 1, 0.1, 3, 0, 0),
        # older than 400 days at D
        ("E", False, "2022-09-30", "2022-11-10", "e1", True, True, 100, 10, 300, 0, 0),
        # equity not positive
        ("G", False, "2023-09-30", "2023-11-10", "g1", True, True, -5, 10, 300, 50, 0),
    ])
    D = pd.Timestamp("2024-02-15")
    aside = {("C", False, pd.Timestamp("2023-09-30"), pd.Timestamp("2023-11-10"))}
    out = cc.balance_sheets_at(f, D, aside, pd.Series({"B": True, "C": False, "E": False, "G": False}))
    assert out.loc["B", "bs_url"] == "b1" and out.loc["B", "debt"] == 0.0     # no borrowings line: no debt
    assert out.loc["C", "bs_url"] == "c1"
    assert out.loc["E", "bs_reason"] == "older_than_400_days" and not out.loc["E", "bs_usable"]
    assert out.loc["G", "bs_reason"] == "equity_not_positive" and not out.loc["G", "bs_usable"]


def test_a_balance_sheet_off_scale_against_most_neighbours_is_excluded():
    rows = []
    for i, pe in enumerate(["2022-09-30", "2023-03-31", "2023-09-30", "2024-03-31"]):
        rows.append(("H", False, pe, f"{pe[:4]}-{int(pe[5:7]) % 12 + 1:02d}-15", f"h{i}", True, True,
                     100, 10, 300, 0, 0))
    rows.append(("H", False, "2024-09-30", "2024-11-10", "h9", True, True, 10000, 1000, 30000, 0, 0))  # 100x
    f = _bs(rows)
    out = cc.balance_sheets_at(f, pd.Timestamp("2025-02-15"), set(), pd.Series({"H": False}))
    assert out.loc["H", "bs_url"] == "h3"
    # only one of assets and share capital out of scale: not a unit error
    rows[-1] = ("H", False, "2024-09-30", "2024-11-10", "h9", True, True, 10000, 10, 30000, 0, 0)
    out = cc.balance_sheets_at(_bs(rows), pd.Timestamp("2025-02-15"), set(), pd.Series({"H": False}))
    assert out.loc["H", "bs_url"] == "h9"


def test_a_usable_balance_sheet_needs_equity_and_equity_plus_debt_above_zero():
    # Clarification after the in-sample engine comparison
    f = _bs([
        ("K", False, "2023-09-30", "2023-11-10", "k1", True, True, 100, 10, 300, -150, 20),   # 100 - 130 < 0
        ("L", False, "2023-09-30", "2023-11-10", "l1", True, True, 100, 10, 300, -100, 0),    # exactly 0
        ("M", False, "2023-09-30", "2023-11-10", "m1", True, True, 100, 10, 300, -99, 0),     # 1 > 0
    ])
    out = cc.balance_sheets_at(f, pd.Timestamp("2024-02-15"), set(), pd.Series({"K": False, "L": False, "M": False}))
    for c in ("K", "L"):
        assert out.loc[c, "bs_reason"] == "capital_not_positive" and not out.loc[c, "bs_usable"]
    assert out.loc["M", "bs_reason"] == "ok" and out.loc["M", "bs_usable"]


def test_set_aside_filings_are_matched_by_stock_basis_period_end_and_broadcast_time():
    # Clarification after the in-sample engine comparison: not by URL. n2 was set
    # aside; n2x is another document with the same four keys (excluded too);
    # n3 revises the same period later (kept); p2 is another stock's filing.
    f = _bs([
        ("N", False, "2023-03-31", "2023-05-20", "n1", True, True, 100, 10, 300, 0, 0),
        ("N", False, "2023-09-30", "2023-11-10", "n2", True, True, 105, 10, 305, 0, 0),
        ("N", False, "2023-09-30", "2023-11-10", "n2x", True, False, 106, 10, 306, 0, 0),
        ("P", False, "2023-09-30", "2023-11-10", "p2", True, True, 100, 10, 300, 0, 0),
    ])
    D = pd.Timestamp("2024-02-15")
    aside = {("N", False, pd.Timestamp("2023-09-30"), pd.Timestamp("2023-11-10"))}
    diag: dict = {}
    out = cc.balance_sheets_at(f, D, aside, pd.Series({"N": False, "P": False}), diag)
    assert out.loc["N", "bs_url"] == "n1" and out.loc["P", "bs_url"] == "p2"
    assert diag["bs_filings_set_aside_by_income_screen"] == 2
    # the other basis is not the set-aside filing
    out = cc.balance_sheets_at(f, D, {("N", True, pd.Timestamp("2023-09-30"), pd.Timestamp("2023-11-10"))},
                               pd.Series({"N": False, "P": False}))
    assert out.loc["N", "bs_url"] == "n2x"
    f2 = _bs([
        ("N", False, "2023-03-31", "2023-05-20", "n1", True, True, 100, 10, 300, 0, 0),
        ("N", False, "2023-09-30", "2023-11-10", "n2", True, True, 105, 10, 305, 0, 0),
        ("N", False, "2023-09-30", "2023-12-01", "n3", True, False, 110, 10, 310, 0, 0),
    ])
    out = cc.balance_sheets_at(f2, D, aside, pd.Series({"N": False}))
    assert out.loc["N", "bs_url"] == "n3"


def test_a_balance_sheet_period_is_its_exact_period_end():
    # Clarification after the in-sample engine comparison: 29 and 30 Sep are
    # two periods, not one calendar quarter, so the later period end wins even
    # though the other was broadcast later.
    f = _bs([
        ("Q", False, "2023-09-30", "2023-11-01", "q30", True, True, 100, 10, 300, 0, 0),
        ("Q", False, "2023-09-29", "2023-11-20", "q29", True, True, 120, 10, 330, 0, 0),
    ])
    out = cc.balance_sheets_at(f, pd.Timestamp("2024-02-15"), set(), pd.Series({"Q": False}))
    assert out.loc["Q", "bs_url"] == "q30" and out.loc["Q", "bs_period_end"] == pd.Timestamp("2023-09-30")


def test_the_balance_sheet_screens_nearest_neighbours_are_nearest_in_days():
    # The balance sheet at 31 Dec 2023, 100x out, against nine others. Its seven
    # nearest are 3, 6 and 9 months either side and 12 before; the eighth is
    # 15 months before (30 Sep 2022, 457 days) or after (31 Mar 2025, 456 days):
    # after, in days. Four of the seven and the one after are on the other
    # scale -> 5 of 8 breaks -> excluded. (By calendar months the tie would go
    # to the earlier, on its own scale: 4 of 8, kept.)
    ends = {"2022-09-30": 100, "2022-12-31": 1, "2023-03-31": 1, "2023-06-30": 1, "2023-09-30": 100,
            "2023-12-31": 100, "2024-03-31": 1, "2024-06-30": 100, "2024-09-30": 100, "2025-03-31": 1}
    b = pd.DataFrame({"cid": "R", "consolidated": False, "period_end": pd.to_datetime(list(ends)),
                      "assets": [300.0 * s for s in ends.values()],
                      "equity_capital": [10.0 * s for s in ends.values()]})
    brk = cc._bs_scale_breaks(b)
    assert bool(brk[b["period_end"] == pd.Timestamp("2023-12-31")].iloc[0])


# ------------------------------------------------------------------ the tranche book
ISINS = {"A": "INE000A01011", "B": "INE000B01011"}


def _panel(n=130, traded=None, prices=None):
    """Two stocks A and B, every session traded unless `traded` says otherwise."""
    sessions = pd.bdate_range("2021-01-04", periods=n)
    rows = []
    for k, sym in enumerate(["A", "B"]):
        for t, d in enumerate(sessions):
            if traded is not None and not traded[sym][t]:
                continue
            px = prices[sym][t] if prices is not None else 100.0 + k
            rows.append((d, sym, "EQ", ISINS[sym], px, px, 1e7))
    p = pd.DataFrame(rows, columns=["date", "symbol", "series", "isin", "open", "close", "turnover"])
    spells = pd.DataFrame({"symbol": ["A", "B"], "isin": [ISINS["A"], ISINS["B"]], "cid": ["A", "B"]})
    ca = pd.DataFrame(columns=["cid", "ex_date", "factor"])
    P = cc.Panels(p, spells, sessions, ca)
    return P


@pytest.fixture(autouse=True)
def _unlock_guard():
    cc.END_DATE = pd.Timestamp("2100-01-01")
    cc.UNLOCK = False
    yield


def _decide_new(weight=0.3):
    def decide(D, held, V):
        sel = ["A"]
        status = {"A": "kept" if "A" in held else "new"}
        return sel, np.array([weight]), status
    return decide


def test_variant_a_buys_three_equal_tranches_at_d_d21_d42():
    P = _panel()
    book = cc.BookV2(P, 0.002, "a", np.zeros((P.T, P.K), bool))
    nav = book.run({0: P.sessions[0]}, _decide_new(0.3), 0, P.T - 1)
    f = pd.DataFrame(book.fills)
    buys = f[f["side"] == "buy"]
    assert list(buys["kind"]) == ["tranche1", "tranche2", "tranche3"]
    assert list(buys["date"]) == [P.sessions[0].date(), P.sessions[21].date(), P.sessions[42].date()]
    assert np.allclose(buys["value"], 500_000 * 0.3 / 3)
    assert np.allclose(buys["cost"], buys["value"] * 0.002)
    assert book.cash == pytest.approx(500_000 * (1 - 0.3 * 1.002))
    assert nav["nav"].iloc[-1] == pytest.approx(500_000 * (1 - 0.3 * 0.002))


def test_variant_a_waits_five_sessions_for_a_trade_then_cancels():
    tr = {"A": [True] * 130, "B": [True] * 130}
    tr["A"][21] = tr["A"][22] = False             # tranche 2 fills at session 23
    for t in range(42, 48):                       # tranche 3: no trade in 42..47 -> cancelled
        tr["A"][t] = False
    P = _panel(traded=tr)
    book = cc.BookV2(P, 0.002, "a", np.zeros((P.T, P.K), bool))
    book.run({0: P.sessions[0]}, _decide_new(), 0, P.T - 1)
    f = pd.DataFrame(book.fills)
    t2 = f[(f["kind"] == "tranche2")]
    assert t2["date"].iloc[0] == P.sessions[23].date()
    c = f[f["kind"] == "cancelled"]
    assert list(c["cancelled_kind"]) == ["tranche3"]
    assert c["date"].iloc[0] == P.sessions[47].date() and c["reason"].iloc[0] == "no_trade_within_5_sessions"


def test_variant_b_waits_for_the_entry_rule_and_buys_the_31st_session_regardless():
    P = _panel()
    ok = np.zeros((P.T, P.K), bool)
    ok[4, 0] = True                               # rule met at the close of session 4 -> tranche 1 at 5
    ok[30, 0] = True                              # close of 30 -> tranche 2 (due 21) buys at 31
    book = cc.BookV2(P, 0.002, "b", ok)           # tranche 3 (due 42) never meets it -> 42 + 30 = 72
    book.run({0: P.sessions[0]}, _decide_new(), 0, P.T - 1)
    f = pd.DataFrame(book.fills)
    got = dict(zip(f["kind"], f["date"]))
    assert got["tranche1"] == P.sessions[5].date()
    assert got["tranche2"] == P.sessions[31].date()
    assert got["tranche3"] == P.sessions[72].date()
    pos = next(iter(book.positions.values()))
    assert pos["tranches"]["tranche3"]["sessions_after_scheduled"] == 30


def test_variant_b_the_scheduled_session_is_the_first_of_the_thirty():
    # Clarification after the in-sample engine comparison: a tranche can buy at
    # the open of its scheduled session s or of any of the next 29 when the
    # close before that open qualifies; otherwise at the open of s + 30.
    P = _panel()
    ok = np.zeros((P.T, P.K), bool)
    ok[20, 0] = True                              # close of 20 -> tranche 2 (due 21) buys at 21 itself
    ok[70, 0] = True                              # close of 70 -> tranche 3 (due 42) buys at 71 = s + 29
    book = cc.BookV2(P, 0.002, "b", ok)
    book.run({0: P.sessions[0]}, _decide_new(), 0, P.T - 1)
    pos = next(iter(book.positions.values()))
    assert pos["tranches"]["tranche2"]["date"] == str(P.sessions[21].date())
    assert pos["tranches"]["tranche2"]["sessions_after_scheduled"] == 0
    assert pos["tranches"]["tranche3"]["date"] == str(P.sessions[71].date())
    assert pos["tranches"]["tranche3"]["sessions_after_scheduled"] == 29
    # tranche 1 (due 0) is still inside its window at 21: the same close qualifies it
    assert pos["tranches"]["tranche1"]["sessions_after_scheduled"] == 21


def test_the_next_decision_date_cancels_a_waiting_tranche_and_resizes_in_full():
    P = _panel()
    book = cc.BookV2(P, 0.002, "b", np.zeros((P.T, P.K), bool))   # the rule never holds
    D2 = 60                                       # tranche 3 (due 42) would wait to 72
    book.run({0: P.sessions[0], D2: P.sessions[D2]}, _decide_new(0.3), 0, P.T - 1)
    f = pd.DataFrame(book.fills)
    c = f[f["kind"] == "cancelled"]
    assert list(c["cancelled_kind"]) == ["tranche3"] and c["reason"].iloc[0] == "next_decision_date"
    assert c["date"].iloc[0] == P.sessions[D2].date()
    r = f[(f["kind"] == "rebalance") & (f["date"] == P.sessions[D2].date())]
    assert len(r) == 1 and r["side"].iloc[0] == "buy"
    held_value = book.sh[0] * 100.0
    assert held_value == pytest.approx(book.nav_open_at[D2] * 0.3)


def test_a_forced_exit_cancels_the_pending_tranches():
    tr = {"A": [True] * 130, "B": [True] * 130}
    for t in range(1, 130):
        tr["A"][t] = False                        # A stops trading after session 0
    P = _panel(traded=tr)
    book = cc.BookV2(P, 0.002, "a", np.zeros((P.T, P.K), bool))
    book.run({0: P.sessions[0]}, _decide_new(), 0, P.T - 1)
    f = pd.DataFrame(book.fills)
    fx = f[f["kind"] == "forced_exit"]
    assert len(fx) == 1 and fx["date"].iloc[0] == P.sessions[20].date()
    c = f[f["kind"] == "cancelled"]
    assert sorted(c["cancelled_kind"]) == ["tranche2", "tranche3"]
    assert set(c["reason"]) == {"position_left_book"}
    assert book.sh[0] == 0.0


def test_a1_metric_is_the_unit_weighted_price_over_the_pre_d_close():
    prices = {"A": np.r_[np.full(10, 90.0), np.full(20, 100.0), np.full(100, 110.0)],
              "B": np.full(130, 50.0)}
    P = _panel(prices=prices)
    D = 10                                        # close before D is 90
    book = cc.BookV2(P, 0.002, "a", np.zeros((P.T, P.K), bool))
    book.run({D: P.sessions[D]}, _decide_new(0.3), D, P.T - 1)
    adj_ff = pd.DataFrame(P.adj).ffill().values
    m = cc.a1_metric(book, P, adj_ff)
    V = 500_000 * 0.3
    units = V / 3 / 100 + V / 3 / 110 + V / 3 / 110      # tranches at 10, 31, 52
    assert m["positions"][0]["relative_price"] == pytest.approx(V / units / 90.0)
    assert m["v_weighted_relative_price"] == pytest.approx(V / units / 90.0)


# ------------------------------------------------------------------ the ROCE-era universe, end to end
def _mini_archive():
    """Eight companies, all standalone, four clean quarters to Dec 2023, priced
    every session from Jun 2022 to the decision date 15 Feb 2024."""
    D = pd.Timestamp("2024-02-15")
    sessions = pd.bdate_range("2022-06-01", D)
    # name: (close, pbt_pre per quarter, balance sheets [(period_end, equity, debt)])
    spec = {
        "GOOD":   (500.0, 1.3e8, [("2023-03-31", 1.8e9, 0.8e9), ("2023-09-30", 2.0e9, 0.8e9)]),
        "LOWROCE": (500.0, 2.5e7, [("2023-09-30", 2.0e9, 0.8e9)]),
        "HIGHDE": (500.0, 1.3e8, [("2023-09-30", 1.0e9, 2.0e9)]),
        "NOBS":   (500.0, 1.3e8, []),
        "OLDBS":  (500.0, 1.3e8, [("2022-09-30", 2.0e9, 0.8e9)]),
        "NEGEQ":  (500.0, 1.3e8, [("2023-09-30", -1.0e9, 0.8e9)]),
        "DEAR":   (3000.0, 1.3e8, [("2023-09-30", 2.0e9, 0.8e9)]),     # P/E 75
        "SMALL":  (50.0, 1.3e8, [("2023-09-30", 2.0e9, 0.8e9)]),       # Rs 50 crore
    }
    rows, fin = [], []
    for i, (sym, (close, pre, bss)) in enumerate(spec.items()):
        isin = f"INE{i:03d}X01011"
        for d in sessions[:-1]:
            rows.append((d, sym, "EQ", isin, close, close, 1e7))
        rows.append((sessions[-1], sym, "EQ", isin, close, np.nan, np.nan))
        for q, pe in enumerate(["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]):
            pe_ = pd.Timestamp(pe)
            bs = [b for b in bss if b[0] == pe]
            fin.append({"cid": sym, "consolidated": False, "period_end": pe_,
                        "broadcast_dt": pe_ + pd.Timedelta(days=40), "xbrl_url": f"{sym}-{pe}",
                        "has_is": True, "has_bs": bool(bs), "taxonomy": "INDAS",
                        "revenue": 1e9, "total_income": 1.01e9, "employee_cost": 1e8, "ebitda": 2e8,
                        "profit_normalised": 1e8, "shares_implied": 1e7,
                        "pbt_before_exceptional": pre, "finance_costs": 1e7, "pbt": pre,
                        "exceptional_items": np.nan,
                        "equity": bs[0][1] if bs else np.nan, "equity_capital": 1e8 if bs else np.nan,
                        "assets": 5e9 if bs else np.nan, "debt_long": bs[0][2] if bs else np.nan,
                        "debt_short": np.nan})
        for pe, eq, debt in bss:
            if pd.Timestamp(pe) < pd.Timestamp("2023-03-31"):           # a balance-sheet-only filing
                pe_ = pd.Timestamp(pe)
                fin.append({"cid": sym, "consolidated": False, "period_end": pe_,
                            "broadcast_dt": pe_ + pd.Timedelta(days=40), "xbrl_url": f"{sym}-{pe}",
                            "has_is": False, "has_bs": True, "taxonomy": "INDAS",
                            **{c: np.nan for c in cc.IS_CARRY}, "total_income": np.nan,
                            "equity": eq, "equity_capital": 1e8, "assets": 5e9, "debt_long": debt,
                            "debt_short": np.nan})
    p = pd.DataFrame(rows, columns=["date", "symbol", "series", "isin", "open", "close", "turnover"])
    spells = p.groupby(["symbol", "isin"], as_index=False).size()[["symbol", "isin"]]
    spells["cid"] = spells["symbol"]
    ca = pd.DataFrame({"cid": pd.Series(dtype=object), "ex_date": pd.Series(dtype="datetime64[ns]"),
                       "factor": pd.Series(dtype=float)})
    P = cc.Panels(p, spells, sessions, ca)
    f = pd.DataFrame(fin)
    f["qn"] = f["period_end"].dt.year * 12 + f["period_end"].dt.month
    lab = pd.DataFrame({"cid": pd.Series(dtype=object), "an_dt": pd.Series(dtype="datetime64[ns]"),
                        "industry": pd.Series(dtype=object)})
    sectors = pd.Series({s: "Capital Goods" for s in spec})
    return D, P, f, lab, ca, sectors


@pytest.mark.parametrize("roce_on", [True, False])
def test_hard_filters_and_the_roce_quality_group_from_february_2023(monkeypatch, roce_on):
    D, P, f, lab, ca, sectors = _mini_archive()
    monkeypatch.setattr(cc, "ROCE_FROM", pd.Timestamp("2023-02-15") if roce_on else pd.Timestamp("2025-02-17"))
    monkeypatch.setattr(cc, "LATEST_NAMES", {})
    df = cc.universe_and_measures(D, P, f, lab, {}, set(), ca, sectors)
    assert df["v1_univ"].all()
    # EBIT = 4 x (pbt before exceptional + finance costs); capital = equity + debt
    assert df.at["GOOD", "ttm_ebit"] == pytest.approx(4 * 1.4e8)
    assert df.at["GOOD", "bs_period_end"] == pd.Timestamp("2023-09-30")      # not the IS-only Dec quarter
    assert df.at["GOOD", "roce_value"] == pytest.approx(5.6e8 / 2.8e9)
    assert df.at["GOOD", "de_value"] == pytest.approx(0.4)
    assert df.at["DEAR", "pe"] == pytest.approx(3000 * 1e7 / 4e8)
    assert not df.at["DEAR", "hf_pe"] and not df.at["SMALL", "hf_mcap"]
    assert df.at["NOBS", "bs_reason"] == "none_known"
    assert df.at["OLDBS", "bs_reason"] == "older_than_400_days"
    assert df.at["NEGEQ", "bs_reason"] == "equity_not_positive"
    u = cc.score(df)
    if roce_on:
        assert set(u.index) == {"GOOD"}
        assert not df.at["LOWROCE", "hf_roce"] and df.at["LOWROCE", "hf_de"]
        assert df.at["HIGHDE", "hf_roce"] and not df.at["HIGHDE", "hf_de"]
        for c in ("NOBS", "OLDBS", "NEGEQ"):
            assert not df.at[c, "hf_roce"] and not df.at[c, "hf_de"] and not df.at[c, "hf_bs"]
        assert (u["quality_basis"] == "roce").all()
        assert u["roce_pct"].notna().all() and u["margin_pct"].isna().all()
    else:
        assert set(u.index) == set(df.index) - {"DEAR", "SMALL"}
        assert df["hf_roce"].all() and df["hf_de"].all()
        assert (u["quality_basis"] == "margin").all()
        assert u["roce"].isna().all() and u["margin_pct"].notna().all()


def test_a_quarter_without_finance_costs_leaves_ttm_ebit_and_roce_missing(monkeypatch):
    # Clarification after the in-sample engine comparison: missing, not 0, so
    # the stock fails the ROCE floor; its debt / equity still stands.
    D, P, f, lab, ca, sectors = _mini_archive()
    f.loc[(f["cid"] == "GOOD") & (f["period_end"] == pd.Timestamp("2023-06-30")), "finance_costs"] = np.nan
    monkeypatch.setattr(cc, "ROCE_FROM", pd.Timestamp("2023-02-15"))
    monkeypatch.setattr(cc, "LATEST_NAMES", {})
    df = cc.universe_and_measures(D, P, f, lab, {}, set(), ca, sectors)
    assert np.isnan(df.at["GOOD", "ttm_ebit"]) and np.isnan(df.at["GOOD", "roce_value"])
    assert bool(df.at["GOOD", "bs_usable"]) and df.at["GOOD", "de_value"] == pytest.approx(0.4)
    assert not df.at["GOOD", "hf_roce"] and df.at["GOOD", "hf_de"] and df.at["GOOD", "hf_bs"]
    assert "GOOD" not in cc.score(df).index


def test_capital_not_positive_fails_both_filters_from_february_2023(monkeypatch):
    D, P, f, lab, ca, sectors = _mini_archive()
    f.loc[(f["cid"] == "GOOD") & (f["period_end"] == pd.Timestamp("2023-09-30")), "debt_long"] = -2.5e9
    monkeypatch.setattr(cc, "ROCE_FROM", pd.Timestamp("2023-02-15"))
    monkeypatch.setattr(cc, "LATEST_NAMES", {})
    df = cc.universe_and_measures(D, P, f, lab, {}, set(), ca, sectors)
    assert df.at["GOOD", "bs_reason"] == "capital_not_positive"
    assert not df.at["GOOD", "hf_bs"] and not df.at["GOOD", "hf_roce"] and not df.at["GOOD", "hf_de"]
    assert np.isnan(df.at["GOOD", "de_value"])


def test_the_income_screens_set_aside_filing_is_found_by_its_four_keys_not_its_url():
    # GOOD's Sep 2023 income statement is 1000x out and set aside. A second
    # document, balance sheet only, has the same stock, basis, period end and
    # broadcast time but its own URL: its balance sheet is set aside too, and
    # the balance sheet falls back to Mar 2023.
    D, P, f, lab, ca, sectors = _mini_archive()
    m = (f["cid"] == "GOOD") & (f["period_end"] == pd.Timestamp("2023-09-30"))
    for c in ("revenue", "total_income", "employee_cost", "shares_implied"):
        f.loc[m, c] = f.loc[m, c] * 1000
    dup = f[m].iloc[[0]].copy()
    dup["xbrl_url"], dup["has_is"] = "GOOD-dup", False
    for c in [*cc.IS_CARRY, "total_income"]:
        dup[c] = np.nan
    f = pd.concat([f, dup], ignore_index=True)
    diag: dict = {}
    fu = cc.fundamentals_at(f, D, ca, diag)
    assert fu.at["GOOD", "bs_period_end"] == pd.Timestamp("2023-03-31")
    assert diag["bs_filings_set_aside_by_income_screen"] == 2


def test_the_equal_weight_benchmark_starts_at_five_lakh_rupees():
    # Clarification after the in-sample engine comparison: like the strategy, as in v1
    P = _panel()
    book = cc.Book(P, 0.002, name="universe_ew")
    nav = book.run({0: (np.array([0, 1]), 2)}, 0, P.T - 1)
    assert nav["nav"].iloc[0] == 500_000.0
    # both names bought at D's open, scaled down so cash covers the costs
    assert nav["nav"].iloc[-1] == pytest.approx(500_000 * (1 - 0.002 / 1.002), rel=1e-12)
    assert book.cash == pytest.approx(0.0, abs=1e-6)


def test_cagr_is_growth_from_the_first_mark():
    dates = pd.Series(pd.to_datetime(["2019-02-15", "2023-02-15"]))
    r = cc.perf(pd.Series([500_000.0, 1_000_000.0]), dates)
    assert r["cagr"] == pytest.approx(2 ** (1 / r["years"]) - 1)

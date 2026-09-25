"""The A5 quality factor (zen/validation/factors.py): sorting, value weighting,
corporate-action returns, and the point-in-time filter.

The point-in-time tests run the real code path (pit.build_snapshot, pit.universe,
factors.formation) on a small synthetic archive in memory, so the filing that
arrives after the month-end is known exactly; one more test checks the same
property on the real archive. No strategy return is computed anywhere here.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import pytest

from zen.universe import pit
from zen.validation import factors as qf


# ---------------------------------------------------------------- sorting
def test_sort_splits_halves_by_median_then_30_40_30_within_each():
    idx = pd.Index([f"S{i:02d}" for i in range(20)])
    mcap = pd.Series(np.arange(1, 21, dtype=float) * 1e9, index=idx)
    # the signal runs against size inside each half, so the halves are not
    # simply the signal order
    sig = pd.Series(np.r_[np.arange(10, 0, -1), np.arange(20, 10, -1)] / 100.0, index=idx)
    lab = qf.sort_2x3(mcap, sig)
    small, big = idx[:10], idx[10:]
    assert set(lab[small].str[0]) == {"S"} and set(lab[big].str[0]) == {"B"}
    for half in (small, big):
        order = sig[half].sort_values().index
        assert list(lab[order[:3]].str[1]) == ["L"] * 3
        assert list(lab[order[3:7]].str[1]) == ["M"] * 4
        assert list(lab[order[7:]].str[1]) == ["H"] * 3


def test_sort_uses_each_half_s_own_breakpoints():
    """A small stock with a middling signal is 'high' among small stocks even
    if every big stock beats it: breakpoints are set within each half."""
    idx = pd.Index(list("abcdefghij"))
    mcap = pd.Series([1, 2, 3, 4, 5, 10, 20, 30, 40, 50], index=idx, dtype=float)
    sig = pd.Series([.01, .02, .03, .04, .05, .50, .60, .70, .80, .90], index=idx)
    lab = qf.sort_2x3(mcap, sig)
    assert lab["e"] == "SH" and lab["a"] == "SL"
    assert lab["f"] == "BL" and lab["j"] == "BH"


def test_sort_skips_missing_signal_and_bad_mcap():
    idx = pd.Index(list("abcdef"))
    mcap = pd.Series([1, 2, np.nan, 4, 0, 6], index=idx, dtype=float)
    sig = pd.Series([.1, np.nan, .3, .4, .5, np.inf], index=idx)
    lab = qf.sort_2x3(mcap, sig)
    assert lab[["b", "c", "e", "f"]].isna().all()
    assert lab[["a", "d"]].notna().all()


# ---------------------------------------------------------------- value weighting
def test_value_weighting_is_by_market_cap():
    ret = pd.Series({"x": 0.10, "y": -0.05, "z": 0.40})
    w = pd.Series({"x": 3e9, "y": 1e9, "z": 0.0})      # z has no weight
    assert qf.value_weighted(ret, w) == pytest.approx((3 * 0.10 + 1 * -0.05) / 4)
    # a stock with no return is dropped and the weights renormalise
    ret2 = ret.copy()
    ret2["y"] = np.nan
    assert qf.value_weighted(ret2, w) == pytest.approx(0.10)


def test_month_row_builds_the_factor_from_the_four_corners():
    lab = pd.Series({"a": "SL", "b": "SL", "c": "SM", "d": "SH", "e": "BL", "f": "BM",
                     "g": "BH", "h": "BH"})
    mcap = pd.Series({"a": 1.0, "b": 3.0, "c": 1.0, "d": 1.0, "e": 5.0, "f": 5.0,
                      "g": 1.0, "h": 9.0})
    ret = pd.Series({"a": 0.08, "b": 0.00, "c": 9.9, "d": 0.03, "e": -0.02, "f": 9.9,
                     "g": 0.10, "h": 0.00})
    row = qf.month_row(lab, mcap, ret)
    sl, sh = (1 * .08 + 3 * 0) / 4, 0.03
    bl, bh = -0.02, (1 * .10 + 9 * 0) / 10
    assert row["SL"] == pytest.approx(sl) and row["BH"] == pytest.approx(bh)
    assert row["factor"] == pytest.approx((sh + bh) / 2 - (sl + bl) / 2)
    assert row["n_SL"] == 2 and row["n_BM"] == 1


# ---------------------------------------------------------------- synthetic archive
CAL = pd.bdate_range("2021-01-01", "2023-03-31")
D = pd.Timestamp("2022-11-30")              # last session of November 2022
N = 10
MARGINS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, -0.10]
REV, DEP, FIN, SHARES, EQUITY, DEBT = 1e9, 1e7, 2e7, 1e7, 4e9, 1e9
INCOME_COLS = ["revenue", "total_income", "other_income", "employee_cost", "ebitda",
               "profit_normalised", "depreciation", "finance_costs",
               "pbt_before_exceptional", "exceptional_items", "pbt"]


def _fin_row(sym, pe, bd, margin, bs=False, equity=EQUITY, debt=DEBT, other_income=0.0,
             exceptional=0.0, income=True):
    """One filing. The archive's identity: ebitda = pre-exceptional profit +
    finance costs + depreciation - other income, so standard EBIT (pre-
    exceptional profit + finance costs) = ebitda - depreciation + other income."""
    rev = REV
    ebitda = margin * rev
    pbe = ebitda - DEP - FIN + other_income
    row = {"symbol": sym, "company": f"{sym} Chemicals Limited",
           "period_end": pd.Timestamp(pe).date(), "broadcast_dt": pd.Timestamp(bd),
           "consolidated": False, "revenue": rev, "total_income": rev + other_income,
           "employee_cost": 0.1 * rev, "other_income": other_income, "ebitda": ebitda,
           "depreciation": DEP, "finance_costs": FIN, "pbt_before_exceptional": pbe,
           "exceptional_items": exceptional, "pbt": pbe + exceptional,
           "profit_normalised": 0.5 * ebitda,
           "shares_implied": SHARES, "equity": equity if bs else None,
           "debt_total": debt if bs else None, "has_balance_sheet": bs,
           "assets": (equity + (debt or 0.0) + 2e9) if bs else None,
           "equity_capital": 1e8 if bs else None,
           "xbrl_url": "https://x/xbrl/INDAS_1_2.xml"}
    if not income:                       # a balance-sheet-only filing (Clarification 38)
        for c in INCOME_COLS:
            row[c] = None
    return row


RUPEE_COLS = INCOME_COLS + ["equity", "debt_total", "assets", "equity_capital"]


def _misscaled(row, x=100.0):
    """The same filing with a unit error: every rupee line and the implied
    share count 100x (EPS unchanged), as a lakhs-tagged-as-rupees filing."""
    r = dict(row)
    for c in RUPEE_COLS + ["shares_implied"]:
        if r.get(c) is not None:
            r[c] = r[c] * x
    return r


def _archive(extra_fin=(), corp=(), prices_fn=None):
    con = duckdb.connect(":memory:")
    px, fin, ann = [], [], []
    for i in range(N):
        sym = f"S{i}"
        for d in CAL:
            close = 100.0 + 10 * i
            if prices_fn:
                close = prices_fn(sym, d, close)
            px.append({"date": d.date(), "symbol": sym, "series": "EQ",
                       "isin_code": f"INE{i:03d}A01011", "open": close, "high": close,
                       "low": close, "close": close, "prev_close": close,
                       "volume": 1e5, "turnover": 1e7, "trades": 100})
        for pe in pd.date_range("2021-03-31", "2022-09-30", freq="QE"):
            bs = pe.month in (3, 9) and pe.year == 2022
            fin.append(_fin_row(sym, pe, pe + pd.Timedelta(days=40), MARGINS[i], bs=bs))
        ann.append({"an_dt": pd.Timestamp("2021-01-05 10:00"), "symbol": sym,
                    "industry": "Chemicals"})
    fin += list(extra_fin)
    prices = pd.DataFrame(px)
    frames = {
        "prices": prices,
        "prices_other": prices.iloc[:0],
        "financials": pd.DataFrame(fin),
        "announcements": pd.DataFrame(ann),
        "corpactions": pd.DataFrame(list(corp), columns=["ex_date", "symbol", "isin", "company",
                                                         "action", "subject", "factor"]),
    }
    if frames["corpactions"].empty:
        frames["corpactions"] = pd.DataFrame({
            "ex_date": pd.Series(dtype="datetime64[ns]"), "symbol": pd.Series(dtype=str),
            "isin": pd.Series(dtype=str), "company": pd.Series(dtype=str),
            "action": pd.Series(dtype=str), "subject": pd.Series(dtype=str),
            "factor": pd.Series(dtype=float)})
    date_cols = {"prices": "date", "prices_other": "date", "corpactions": "ex_date",
                 "financials": "period_end"}
    for name, df in frames.items():
        df = df.copy()
        if name in date_cols:
            df[date_cols[name]] = pd.to_datetime(df[date_cols[name]])
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
        if name in date_cols:        # the archive stores these as DATE
            con.execute(f"ALTER TABLE {name} ALTER {date_cols[name]} TYPE DATE")
    return con


def _ttm_roce(margin, other_income=0.0):
    return 4 * (margin * REV - DEP + other_income) / (EQUITY + DEBT)


def test_filing_broadcast_after_month_end_is_never_used():
    """S0 restates its September 2022 quarter twice after the November
    formation: once on the formation session itself and once the next day,
    each with a 90% margin and a tiny equity. Neither may reach the formation.
    S1 restates the same quarter the evening BEFORE the formation session,
    which must be used (the filter is not simply stale)."""
    sep = pd.Timestamp("2022-09-30")
    late = [
        _fin_row("S0", sep, D + pd.Timedelta(hours=10), 0.90, bs=True, equity=1e6),
        _fin_row("S0", sep, D + pd.Timedelta(days=1, hours=9), 0.90, bs=True, equity=1e6),
        _fin_row("S1", sep, D - pd.Timedelta(hours=6), 0.30, bs=True),
    ]
    con = _archive(extra_fin=late)
    static = pit.StaticLabels.load(con)
    f = qf.formation(con, D, static).set_index("symbol")
    assert len(f) == N                                   # the loss-maker is in the frame,
    assert not f.at["S9", "r6_pass"]                     # outside v1's universe (rule 6),
    assert pd.isna(f.at["S9", "label_margin"])           # so not in the headline sort,
    assert f.at["S9", "label_margin_no_r6"] == "BL"      # only in the robustness line
    assert f.at["S0", "margin"] == pytest.approx(0.05)
    assert f.at["S0", "roce"] == pytest.approx(_ttm_roce(0.05))
    assert f.at["S0", "capital"] == pytest.approx(EQUITY + DEBT)
    assert (pd.to_datetime(f["latest_broadcast"]) < D).all()
    # the pre-month-end restatement is used: three quarters at 10%, one at 30%
    assert f.at["S1", "margin"] == pytest.approx((3 * 0.10 + 0.30) / 4)
    assert f.at["S1", "roce"] == pytest.approx(
        (3 * (0.10 * REV - DEP) + (0.30 * REV - DEP)) / (EQUITY + DEBT))


def test_extras_and_balance_sheets_read_nothing_at_or_after_the_formation_date():
    sep = pd.Timestamp("2022-09-30")
    late = [_fin_row("S2", sep, D + pd.Timedelta(days=1), 0.9, bs=True, equity=1.0),
            _fin_row("S3", sep, D + pd.Timedelta(hours=1), 0.9, bs=True, equity=1.0,
                     income=False)]
    con = _archive(extra_fin=late)
    static = pit.StaticLabels.load(con)
    snap = pit.build_snapshot(con, D, static)
    qx = qf.quarter_extras(con, snap, static.ids)
    assert (qx["broadcast_dt"] < D).all()
    assert not (qx["pbt_before_exceptional"] == 0.9 * REV - DEP - FIN).any()
    bs = qf.latest_balance_sheets(con, snap, static.ids)
    assert (pd.to_datetime(bs["broadcast_dt"]) < D).all()
    assert not (bs["equity"] == 1.0).any()


def test_balance_sheet_needed_for_roce_and_margin_works_without():
    """Before any balance sheet is known ROCE is missing; the margin is not."""
    con = _archive()
    static = pit.StaticLabels.load(con)
    f = qf.formation(con, pd.Timestamp("2022-04-29"), static).set_index("symbol")
    assert f["roce"].isna().all()
    assert f["margin"].notna().all()
    assert f["label_roce"].isna().all()          # before February 2023: no ROCE sort
    assert f["label_roce_no_r6"].isna().all()


def test_roce_sort_starts_with_the_february_2023_formation():
    """Balance sheets exist from the September 2022 quarter, so ROCE is finite
    at the November 2022 and January 2023 formations, but the ROCE sort only
    switches on at the February 2023 formation."""
    con = _archive()
    static = pit.StaticLabels.load(con)
    for d in ("2022-11-30", "2023-01-31"):
        f = qf.formation(con, pd.Timestamp(d), static)
        assert f["roce"].notna().sum() >= N - 1
        assert f["label_roce"].isna().all() and f["label_roce_no_r6"].isna().all()
    f = qf.formation(con, pd.Timestamp("2023-02-28"), static)
    assert f["label_roce"].notna().sum() == N - 1      # v1 universe: S9 fails rule 6
    assert f["label_roce_no_r6"].notna().sum() == N


def test_roce_uses_standard_ebit_with_other_income_and_without_exceptional_items():
    """S2 restates its four TTM quarters before the formation with Rs 5 crore
    of other income and a Rs 3 crore exceptional loss each quarter. ROCE's EBIT
    is pre-exceptional profit + finance costs: other income in, exceptional
    items out, finance costs added back."""
    oi, exc = 5e7, -3e7
    qs = pd.date_range("2021-12-31", "2022-09-30", freq="QE")
    restated = [_fin_row("S2", pe, D - pd.Timedelta(days=5), MARGINS[2], bs=(pe.month == 9),
                         other_income=oi, exceptional=exc) for pe in qs]
    con = _archive(extra_fin=restated)
    static = pit.StaticLabels.load(con)
    f = qf.formation(con, D, static).set_index("symbol")
    assert f.at["S2", "ttm_ebit"] == pytest.approx(4 * (MARGINS[2] * REV - DEP + oi))
    assert f.at["S2", "roce"] == pytest.approx(_ttm_roce(MARGINS[2], other_income=oi))


def test_missing_pre_exceptional_profit_falls_back_to_pbt_less_exceptional_items():
    liq = pd.DataFrame({"consolidated": [False], "latest_period_end": [pd.Timestamp("2022-09-30")]},
                       index=pd.Index(["X"], name="symbol"))
    qs = pd.date_range("2021-12-31", "2022-09-30", freq="QE")
    q = pd.DataFrame({"symbol": "X", "consolidated": False, "period_end": qs,
                      "q": qs.year * 4 + (qs.month - 1) // 3,
                      "pbt_before_exceptional": [10.0, 10.0, 10.0, np.nan],
                      "pbt": [7.0, 7.0, 7.0, 4.0], "exceptional_items": [-3.0, -3.0, -3.0, -6.0],
                      "finance_costs": [1.0, 1.0, 1.0, 1.0]})
    assert qf.ttm_ebit(liq, q)["X"] == pytest.approx(3 * 11.0 + (4.0 + 6.0 + 1.0))
    q.loc[3, "finance_costs"] = np.nan                 # a quarter missing a line: no TTM
    assert np.isnan(qf.ttm_ebit(liq, q)["X"])


def test_balance_sheet_is_chosen_separately_from_the_income_statement():
    """Clarification 38. S4 files a balance-sheet-only revision of its September
    2022 quarter before the formation: its balance sheet is the one used. S5
    restates its September income statement with no balance sheet: the income
    figures come from the restatement, the balance sheet from the earlier
    filing of the same quarter (not the older March one)."""
    sep = pd.Timestamp("2022-09-30")
    extra = [_fin_row("S4", sep, D - pd.Timedelta(days=3), MARGINS[4], bs=True,
                      equity=2e9, income=False),
             _fin_row("S5", sep, D - pd.Timedelta(days=3), 0.50, bs=False)]
    con = _archive(extra_fin=extra)
    static = pit.StaticLabels.load(con)
    f = qf.formation(con, D, static).set_index("symbol")
    assert f.at["S4", "capital"] == pytest.approx(2e9 + DEBT)
    assert f.at["S4", "margin"] == pytest.approx(MARGINS[4])   # income side untouched
    assert f.at["S5", "capital"] == pytest.approx(EQUITY + DEBT)
    assert f.at["S5", "bs_period_end"] == sep
    assert f.at["S5", "margin"] == pytest.approx((3 * MARGINS[5] + 0.50) / 4)


def test_balance_sheet_order_scale_screens_and_default_start():
    """Four guards the independent check asked for.
    S6: a mis-scaled restatement of September 2022 that pit's income screen
        drops; its balance sheet is not used, the earlier September filing is.
    S7: a late balance-sheet-only restatement of MARCH 2022, broadcast after the
        September filing; the later period still wins (period, then revision).
    S8: a balance-sheet-only September 2022 revision with assets and share
        capital 100x; the balance-sheet screen drops it and the period falls
        back to March 2022, as pit treats a mis-scaled quarter.
    And the default first formation is the February 2019 month-end."""
    sep, mar = pd.Timestamp("2022-09-30"), pd.Timestamp("2022-03-31")
    extra = [_misscaled(_fin_row("S6", sep, D - pd.Timedelta(days=4), MARGINS[6], bs=True)),
             _fin_row("S7", mar, D - pd.Timedelta(days=2), MARGINS[7], bs=True,
                      equity=1e9, income=False),
             _misscaled(_fin_row("S8", sep, D - pd.Timedelta(days=3), MARGINS[8], bs=True,
                                 income=False))]
    # two older balance sheets on the right scale, so a majority can decide for S8
    extra += [_fin_row("S8", pe, pe + pd.Timedelta(days=40), MARGINS[8], bs=True, income=False)
              for pe in (pd.Timestamp("2021-03-31"), pd.Timestamp("2021-09-30"))]
    con = _archive(extra_fin=extra)
    static = pit.StaticLabels.load(con)
    snap = pit.build_snapshot(con, D, static)
    assert "S6" in set(snap.scale_dropped["symbol"])            # pit's income screen fires
    f = qf.formation(con, D, static).set_index("symbol")
    assert f.at["S6", "capital"] == pytest.approx(EQUITY + DEBT)
    assert f.at["S6", "bs_period_end"] == sep
    assert f.at["S7", "capital"] == pytest.approx(EQUITY + DEBT)
    assert f.at["S7", "bs_period_end"] == sep
    assert f.at["S8", "capital"] == pytest.approx(EQUITY + DEBT)
    assert f.at["S8", "bs_period_end"] == mar
    cal = pd.DatetimeIndex(pd.bdate_range("2019-01-01", "2019-04-30"))
    assert qf.formation_sessions(cal)[0] == pd.Timestamp("2019-02-28")


def test_balance_sheet_scale_screen_needs_both_lines_to_break():
    """A genuine 40x jump in assets with share capital unchanged is kept; the
    same jump with share capital also 40x is a unit error and is dropped."""
    b = pd.DataFrame({"symbol": "X", "consolidated": False,
                      "period_end": pd.to_datetime(["2023-03-31", "2023-09-30", "2024-03-31",
                                                    "2024-09-30"]),
                      "assets": [1e9, 1e9, 1e9, 4e10], "equity_capital": [1e7, 1e7, 1e7, 1e7]})
    assert not qf.balance_sheet_scale_errors(b).any()
    b.loc[3, "equity_capital"] = 4e8
    assert list(qf.balance_sheet_scale_errors(b)) == [False, False, False, True]
    # two periods that disagree, nothing else to judge by: neither is trusted
    two = b.iloc[2:].reset_index(drop=True)
    assert list(qf.balance_sheet_scale_errors(two)) == [True, True]


def test_balance_sheet_age_equity_and_missing_borrowings_rules():
    D2 = pd.Timestamp("2023-03-31")
    liq = pd.DataFrame({"consolidated": False, "latest_period_end": pd.Timestamp("2022-12-31"),
                        "ttm_revenue": 100.0, "ttm_ebitda": 20.0},
                       index=pd.Index(["old", "neg", "nodebt", "ok"], name="symbol"))
    bs = pd.DataFrame({"symbol": ["old", "neg", "nodebt", "ok"], "consolidated": False,
                       "period_end": [D2 - pd.Timedelta(days=401), pd.Timestamp("2022-12-31"),
                                      pd.Timestamp("2022-12-31"), D2 - pd.Timedelta(days=400)],
                       "broadcast_dt": pd.Timestamp("2023-02-10"),
                       "equity": [50.0, -5.0, 80.0, 60.0], "debt_total": [10.0, 10.0, np.nan, 40.0]})
    s = qf.signals(liq, pd.DataFrame(), bs, D2)
    assert np.isnan(s.at["old", "capital"])            # more than 400 days old
    assert np.isnan(s.at["neg", "capital"])            # equity not positive
    assert s.at["nodebt", "capital"] == pytest.approx(80.0)   # no borrowings line: zero debt
    assert s.at["ok", "capital"] == pytest.approx(100.0)      # exactly 400 days is allowed


# ---------------------------------------------------------------- returns
def test_holding_return_survives_a_split_and_includes_the_dividend():
    """S3: 2:1 split ex 2022-12-12 (raw close 130 -> 65), Rs 1 dividend ex
    2022-12-19 on a 65 close, 71.5 at the end of December. The December return
    is (65+1)/65 x 71.5/65 - 1, not the -45% the raw closes show."""
    split, exdiv = pd.Timestamp("2022-12-12"), pd.Timestamp("2022-12-19")

    def prices(sym, d, close):
        if sym != "S3" or d < split:
            return close
        return 71.5 if d >= pd.Timestamp("2022-12-30") else close / 2

    corp = [(split.date(), "S3", "INE003A01011", "S3", "split",
             "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", 0.5),
            (exdiv.date(), "S3", "INE003A01011", "S3", "dividend",
             "Interim Dividend - Rs 1 Per Share", None)]
    con = _archive(corp=corp, prices_fn=prices)
    static = pit.StaticLabels.load(con)
    panel = qf.build_return_panel(con, pd.Timestamp("2022-11-01"),
                                  pd.Timestamp("2023-01-31"), static.ids)
    r = panel.holding_returns(D, pd.Timestamp("2022-12-30"), ["S3", "S4"])
    assert r["S3"] == pytest.approx((66 / 65) * (71.5 / 65) - 1)
    assert r["S4"] == pytest.approx(0.0)


def test_monthly_factor_earns_the_next_month_only():
    """A formation at the November month-end earns December, close to close:
    S0's jump in the formation month must not appear, S1's jump in December
    must, and the month is labelled 2022-12."""
    def prices(sym, d, close):
        if sym == "S0" and d >= pd.Timestamp("2022-11-15"):
            return close * 1.5
        if sym == "S1" and d >= pd.Timestamp("2022-12-15"):
            return close * 1.2
        return close

    con = _archive(prices_fn=prices)
    static = pit.StaticLabels.load(con)
    cal = pit.sessions(con)
    forms = qf.formation(con, D, static)
    panel = qf.build_return_panel(con, D, cal[-1], static.ids)
    fac, hold = qf.monthly_factor(forms, panel, cal)
    assert set(fac["month"]) == {"2022-12"}
    row = fac[fac["version"] == "margin"].iloc[0]
    assert row["start"] == D and row["end"] == pd.Timestamp("2022-12-30") and row["complete"]
    h = hold.set_index("symbol")
    assert h.at["S0", "ret"] == pytest.approx(0.0)
    assert h.at["S1", "ret"] == pytest.approx(0.2)
    assert row["n_universe"] == N - 1                  # v1 universe excludes the loss-maker
    assert fac.loc[fac["version"] == "margin_no_r6", "n_universe"].iloc[0] == N


def test_size_breakpoint_uses_the_whole_universe():
    """Ten names; the five largest are big even though the three smallest have
    no signal (the median is taken over all ten, not over the seven sorted)."""
    idx = pd.Index(list("abcdefghij"))
    mcap = pd.Series(np.arange(1, 11, dtype=float), index=idx)
    sig = pd.Series([np.nan, np.nan, np.nan, .1, .2, .3, .4, .5, .6, .7], index=idx)
    lab = qf.sort_2x3(mcap, sig)
    assert set(lab.dropna().index[lab.dropna().str[0] == "B"]) == set("fghij")
    assert lab[["a", "b", "c"]].isna().all()


def test_formation_sessions_are_month_ends():
    cal = pd.DatetimeIndex(pd.bdate_range("2019-03-01", "2019-05-31"))
    got = qf.formation_sessions(cal, (2019, 3), (2019, 5))
    assert got == [pd.Timestamp("2019-03-29"), pd.Timestamp("2019-04-30"),
                   pd.Timestamp("2019-05-31")]


# ---------------------------------------------------------------- real archive
@pytest.fixture(scope="module")
def real():
    try:
        con = pit.connect(read_only=True)
    except Exception as e:                              # another process holds a write lock
        pytest.skip(f"archive not readable: {e}")
    yield con, pit.StaticLabels.load(con)
    con.close()


def test_real_formation_is_point_in_time_and_reproduces_pit(real):
    """At the March 2023 formation, the rule-6-free universe must reproduce
    pit.universe once rule 6 is put back (formation() asserts this), every
    fundamental used was broadcast before the formation session, and loss-makers
    are in it."""
    con, static = real
    Dr = pd.Timestamp("2023-03-31")
    f = qf.formation(con, Dr, static)
    assert len(f) > 500
    assert (pd.to_datetime(f["latest_broadcast"]) < Dr).all()
    assert (~f["r6_pass"]).sum() > 50
    bsb = pd.to_datetime(f["bs_broadcast"]).dropna()   # the time filter is the broadcast
    assert len(bsb) > 300 and (bsb < Dr).all()
    assert f["label_roce"].notna().sum() > 300
    assert f.loc[~f["r6_pass"], "label_roce"].isna().all()


# ---------------------------------------------------------------- build cache
def test_formation_cache_is_rebuilt_when_the_database_changes(tmp_path):
    import json
    from jobs import build_quality_factor as bq
    db = tmp_path / "x.duckdb"
    db.write_bytes(b"one")
    cache = tmp_path / "cache"
    cache.mkdir()
    fp = bq.fingerprint(db)
    (cache / "fingerprint.json").write_text(json.dumps(fp), encoding="utf-8")
    assert bq.cache_is_current(cache, fp)
    db.write_bytes(b"two, longer")                     # the database changed
    assert not bq.cache_is_current(cache, bq.fingerprint(db))
    assert not bq.cache_is_current(tmp_path / "never_built", fp)
    assert set(bq.fingerprint(db)) >= {"db_size", "db_mtime_ns", "factors.py", "pit.py", "engine.py"}

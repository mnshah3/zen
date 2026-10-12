"""Strategy v3 engine (zen/portfolio/engine_v3.py) on small synthetic books.

Covers the spec's eligibility filters (section 5) and the figures behind them
(sections 3.3a to 4.9), the ranking and its tie rules (section 6), the 18 slots
and the sector cap (section 7), the exits (section 8), the valuation trim and
the 10% cap (section 9, including the owner's worked example 9.3), the review
order (section 10) and the order execution of 9.4 and 11. No database.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from zen.portfolio import engine, engine_v3 as v3

D = pd.Timestamp("2021-02-15")
D_DE = pd.Timestamp("2022-11-15")
CR = 1e7


# ---------------------------------------------------------------- synthetic quarters
def qrows(sym, cons=False, n=9, last="2020Q4", **over):
    """n quarters ending at `last` for one stock and basis; every figure is a
    constant unless given as a list (oldest first) in `over`."""
    pe = pd.period_range(end=last, periods=n, freq="Q").end_time.normalize()
    base = {"revenue": 100 * CR, "profit_normalised": 10 * CR, "ebitda": 15 * CR,
            "pbt_before_exceptional": 13 * CR, "pbt": 13 * CR, "exceptional_items": 0.0,
            "finance_costs": 1 * CR, "eps_basic": 1.0, "profit_continuing": 10 * CR,
            "shares_implied": 1e8, "total_income": 100 * CR, "employee_cost": 5 * CR,
            "other_income": 0.0}
    rows = []
    for i, p in enumerate(pe):
        r = {"symbol": sym, "consolidated": cons, "period_end": p,
             "broadcast_dt": p + pd.Timedelta(days=40)}
        for k, v in base.items():
            r[k] = v
        for k, v in over.items():
            r[k] = v[i] if isinstance(v, (list, tuple, np.ndarray)) else v
        rows.append(r)
    df = pd.DataFrame(rows)
    df["q"] = v3.quarter_index(df["period_end"])
    return df


def fund_for(q, cons=None):
    rows = {}
    for sym, g in q.groupby("symbol"):
        c = bool(g["consolidated"].any()) if cons is None else cons.get(sym, False)
        gg = g[g["consolidated"] == c]
        rows[sym] = {"consolidated": c,
                     "latest_period_end": gg["period_end"].max() if len(gg) else pd.NaT,
                     "shares": 1e8, "shares_fix": ""}
    return pd.DataFrame.from_dict(rows, orient="index")


def caps_for(syms, mcap):
    m = pd.Series(mcap, index=syms) if np.isscalar(mcap) else pd.Series(mcap)
    df = pd.DataFrame(index=pd.Index(syms, name="symbol"))
    df["close"] = m / 1e8
    df["close_factor"] = 1.0
    df["close_adj"] = df["close"]
    df["close_date"] = D - pd.Timedelta(days=3)
    df["close_series"] = "EQ"
    df["close_ticker"] = df.index
    df["shares"] = 1e8
    df["shares_fix"] = ""
    df["mcap"] = m
    return df


def figs(q, mcap=800 * CR, bs=None, at=D, members=None, cons=None):
    syms = sorted(q["symbol"].unique())
    members = pd.Index(syms if members is None else members)
    return v3.stock_figures(q, fund_for(q, cons), caps_for(syms, mcap), bs, at, members)


# ---------------------------------------------------------------- figures and filters
def test_growth_floor_and_formula():
    # Rs 1 crore to Rs 30 crore over two quarters: the base is Rs 12.5 crore, not 1
    assert v3.growth(30 * CR, 1 * CR, v3.BASE_2Q) == pytest.approx(29 / 12.5)
    assert v3.growth(150 * CR, 100 * CR, v3.BASE_4Q) == pytest.approx(0.5)
    assert v3.growth(10 * CR, -5 * CR, v3.BASE_4Q) == pytest.approx(15 / 25)   # loss to profit


def test_baseline_stock_is_eligible_with_exact_measures():
    rev = [100, 100, 100, 100, 110, 120, 130, 150, 170]           # oldest first, crore
    q = qrows("A", revenue=[r * CR for r in rev])
    f = figs(q).loc["A"]
    assert f["basis"] == "standalone" and f["L"] == "2020-12" and f["run"] == 9
    assert f["missing_quarters"] == ""
    R = [r * CR for r in rev[::-1]]                                # R_0 is the latest
    want = (v3.growth(R[0] + R[1], R[4] + R[5], v3.BASE_2Q)
            - v3.growth(R[2] + R[3], R[6] + R[7], v3.BASE_2Q))
    assert f["A_R"] == want
    assert f["ttm_p"] == 40 * CR and f["pe"] == pytest.approx(20.0)
    assert f["icr_state"] == "icr" and f["icr"] == pytest.approx(14.0)
    assert bool(f["eligible"]) and all(bool(f[k]) for k in ("f0", "f1", "f2", "f3", "f4",
                                                             "f5", "f6"))


def test_basis_prefers_complete_consolidated_then_standalone():
    sa = qrows("A", cons=False, n=9)
    co_short = qrows("A", cons=True, n=5)                  # consolidated too young
    co_full = qrows("B", cons=True, n=8)
    sa_b = qrows("B", cons=False, n=9)
    q = pd.concat([sa, co_short, co_full, sa_b], ignore_index=True)
    f = figs(q, cons={"A": True, "B": True})
    assert f.at["A", "basis"] == "standalone" and f.at["A", "c4_basis"] == "consolidated"
    assert f.at["B", "basis"] == "consolidated"
    assert bool(f.at["A", "f5"]) and bool(f.at["B", "f5"])


def test_missing_quarter_fails_history_and_is_listed():
    q = qrows("A")
    q = q[q["period_end"] != pd.Timestamp("2020-06-30")]
    f = figs(q).loc["A"]
    assert not f["complete"] and not bool(f["f5"]) and not bool(f["eligible"])
    assert f["run"] == 2 and "2020-06" in f["missing_quarters"].split(";")


def test_profit_floor_f1():
    q = pd.concat([qrows("A", profit_normalised=6.25 * CR),            # TTM exactly 25 cr
                   qrows("B", profit_normalised=6.2 * CR)], ignore_index=True)
    f = figs(q, mcap=200 * CR)
    assert bool(f.at["A", "f1"]) and not bool(f.at["B", "f1"])


def test_interest_coverage_states_and_write_back():
    fc_wb = [1 * CR] * 8 + [-2 * CR]                      # latest quarter writes back 2 cr
    q = pd.concat([qrows("WB", finance_costs=fc_wb),
                   qrows("NOFC", finance_costs=0.0),
                   qrows("EDGE", pbt_before_exceptional=1.5 * CR, finance_costs=1 * CR),
                   qrows("LOW", pbt_before_exceptional=1.49 * CR, finance_costs=1 * CR),
                   qrows("UNK", finance_costs=[1 * CR] * 8 + [np.nan])], ignore_index=True)
    f = figs(q)
    assert f.at["WB", "icr_state"] == "write_back" and bool(f.at["WB", "write_back"])
    assert not bool(f.at["WB", "f2"])
    assert f.at["WB", "fc_pos"] == 3 * CR                 # positive finance costs only
    assert f.at["NOFC", "icr_state"] == "no_finance_costs" and bool(f.at["NOFC", "f2"])
    assert f.at["EDGE", "icr"] == pytest.approx(2.5) and bool(f.at["EDGE", "f2"])
    assert not bool(f.at["EDGE", "e2"])
    assert not bool(f.at["LOW", "f2"]) and bool(f.at["LOW", "e2"])
    assert f.at["UNK", "icr_state"] == "unknown" and not bool(f.at["UNK", "e2_known"])


def test_write_back_does_not_by_itself_trigger_e2():
    q = qrows("WB", finance_costs=[1 * CR] * 8 + [-2 * CR])
    f = figs(q).loc["WB"]
    assert not bool(f["f2"]) and bool(f["e2_known"]) and not bool(f["e2"])


def _bs(sym, equity, debt, period="2022-09-30", cons=False):
    return pd.DataFrame([{"symbol": sym, "consolidated": cons,
                          "period_end": pd.Timestamp(period),
                          "broadcast_dt": pd.Timestamp(period) + pd.Timedelta(days=40),
                          "equity": equity, "debt_total": debt}])


def test_debt_to_equity_from_november_2022_only():
    q = pd.concat([qrows(s, last="2022Q3") for s in ("AT", "OVER", "NEG", "NONE")],
                  ignore_index=True)
    bs = pd.concat([_bs("AT", 100 * CR, 120 * CR), _bs("OVER", 100 * CR, 121 * CR),
                    _bs("NEG", -1 * CR, 0.0)], ignore_index=True)
    f = figs(q, bs=bs, at=D_DE)
    assert f.at["AT", "de"] == pytest.approx(1.2)
    assert not bool(f.at["AT", "f3"]) and not bool(f.at["AT", "e3"])      # 1.2 blocks, no sale
    assert not bool(f.at["OVER", "f3"]) and bool(f.at["OVER", "e3"])
    assert not bool(f.at["NEG", "f3"]) and bool(f.at["NEG", "e3"])       # equity <= 0
    assert bool(f.at["NONE", "f3"]) and not bool(f.at["NONE", "e3_known"])
    before = figs(q, bs=bs, at=pd.Timestamp("2022-08-16"))
    assert before["f3"].all() and not before["e3"].any()


def test_stale_balance_sheet_is_not_usable():
    q = qrows("A", last="2022Q3")
    f = figs(q, bs=_bs("A", 100 * CR, 500 * CR, period="2021-06-30"), at=D_DE).loc["A"]
    assert not bool(f["bs_usable"]) and bool(f["f3"])


def test_pe_ceiling_f4():
    q = pd.concat([qrows("A"), qrows("B")], ignore_index=True)
    f = figs(q, mcap={"A": 60 * 40 * CR, "B": 60.0001 * 40 * CR})
    assert bool(f.at["A", "f4"]) and not bool(f.at["B", "f4"])


def test_growth_reversal_flags_and_f6():
    falling = [12, 12, 12, 12, 12, 10, 10, 10, 10]       # P oldest first, crore
    q = pd.concat([qrows("DOWN", profit_normalised=[p * CR for p in falling]),
                   qrows("SHORT", n=8, profit_normalised=[p * CR for p in falling[1:]])],
                  ignore_index=True)
    f = figs(q, mcap=400 * CR)
    assert f.at["DOWN", "down0"] == True and f.at["DOWN", "down1"] == True   # noqa: E712
    assert not bool(f.at["DOWN", "f6"]) and bool(f.at["DOWN", "down_both"])
    assert pd.isna(f.at["SHORT", "down1"])                  # Q_8 missing: unknown
    assert bool(f.at["SHORT", "f6"])                        # unknown counts as false


def test_results_overdue_e5():
    q = qrows("OLD", last="2020Q2")                          # 30 Jun 2020 vs 15 Feb 2021
    f = figs(q).loc["OLD"]
    assert bool(f["e5"])
    assert not bool(figs(qrows("NEW")).loc["NEW", "e5"])


# ---------------------------------------------------------------- ranking
def _elig(rows):
    e = pd.DataFrame(rows).set_index("symbol")
    e["sector_key"] = "x"
    return e


def test_ranks_are_whole_and_half_ranks_and_ties_go_to_ticker():
    e = _elig([
        {"symbol": "S1", "ticker": "BBB", "A_R": 0.1, "A_P": 0.1, "M": 0.01, "g": 0.2,
         "PEG": 1.0, "pe": 20.0},
        {"symbol": "S2", "ticker": "AAA", "A_R": 0.1, "A_P": 0.1, "M": 0.01, "g": 0.2,
         "PEG": 1.0, "pe": 20.0},
        {"symbol": "S3", "ticker": "CCC", "A_R": 0.3, "A_P": 0.0, "M": 0.02, "g": -0.1,
         "PEG": np.nan, "pe": 20.0},
        {"symbol": "S4", "ticker": "DDD", "A_R": -0.2, "A_P": 0.5, "M": -0.01, "g": 0.6,
         "PEG": 0.5, "pe": 30.0},
    ])
    r = v3.rank_eligible(e)
    # r(A_R): S4 1, S1/S2 2.5, S3 4; r(A_P): S3 1, S1/S2 2.5, S4 4
    assert r.at["S1", "r_A_R"] == 2.5 and r.at["S3", "r_A_P"] == 1.0
    # R3: one non-positive growth name gets (1 + 1) / 2; positive: 1 + rank of -PEG
    assert r.at["S3", "R3"] == 1.0
    assert r.at["S4", "R3"] == 1 + 3.0 and r.at["S1", "R3"] == 1 + 1.5
    assert (r["T"] == r["R1"] + r["R2"] + r["R3"]).all()
    # S1 and S2 tie on T; the ticker decides (AAA before BBB)
    assert r.at["S1", "T"] == r.at["S2", "T"]
    assert r.index.get_loc("S2") < r.index.get_loc("S1")
    assert list(r["rank"]) == [1, 2, 3, 4]
    assert r.at["S1", "S"] == pytest.approx(r.at["S1", "T"] / 12)


def test_tie_on_ticker_goes_to_stock_id():
    e = _elig([{"symbol": s, "ticker": "SAME", "A_R": 0.0, "A_P": 0.0, "M": 0.0, "g": 0.1,
                "PEG": 2.0, "pe": 20.0} for s in ("ZZ", "AA")])
    assert list(v3.rank_eligible(e).index) == ["AA", "ZZ"]


def test_missing_measure_stops_the_ranking():
    e = _elig([{"symbol": "A", "ticker": "A", "A_R": np.nan, "A_P": 0.0, "M": 0.0, "g": 0.1,
                "PEG": 2.0, "pe": 20.0}])
    with pytest.raises(ValueError):
        v3.rank_eligible(e)


# ---------------------------------------------------------------- the review plan
def book_figs(rows: dict) -> pd.DataFrame:
    """Per-stock exit and filter inputs for plan_review; defaults pass everything."""
    base = {"pe": 20.0, "e1": False, "e1_known": True, "e2": False, "e2_known": True,
            "e3_on": False, "e3": False, "e3_known": False, "down_both": False,
            "down_known": True, "down0": False, "down1": False, "e5": False,
            "in_universe": True, "eligible": True}
    out = []
    for sym, over in rows.items():
        r = {"symbol": sym, "ticker": sym, **base, **over}
        out.append(r)
    return pd.DataFrame(out).set_index("symbol")


def ranks_of(syms):
    r = pd.DataFrame(index=pd.Index(syms, name="symbol"))
    r["rank"] = np.arange(1, len(syms) + 1)
    return r


def hold_of(rows: dict) -> pd.DataFrame:
    h = pd.DataFrame.from_dict(rows, orient="index")
    for c, d in (("U0", np.nan), ("held_prev", False)):
        if c not in h:
            h[c] = d
    h.index.name = "symbol"
    return h


def test_first_review_fills_18_slots_with_the_cost_inside_the_capital():
    syms = [f"C{i:02d}" for i in range(25)]
    keys = pd.Series({s: f"sec{i % 7}" for i, s in enumerate(syms)})
    p = v3.plan_review(D, hold_of({}).reindex(columns=["units", "px_now", "U0", "held_prev"]),
                       book_figs({s: {} for s in syms}), ranks_of(syms), keys, 5e5, 5e5)
    buys = [o for o in p.orders if o["kind"] == "buy"]
    assert len(buys) == 18 and [o["symbol"] for o in buys] == syms[:18]
    assert buys[0]["value"] == pytest.approx(5e5 / 18)
    assert buys[-1]["value"] == pytest.approx(26780, abs=1)          # the spec's Rs 26,780
    assert p.decision["slots_left_empty"] == 0 and p.decision["empty_reasons"] == ""


def test_sector_cap_of_four_counts_kept_holdings_and_buys():
    syms = [f"C{i}" for i in range(8)]
    keys = pd.Series({"H1": "metal", **{s: ("metal" if i < 5 else "other")
                                        for i, s in enumerate(syms)}})
    hold = hold_of({"H1": {"units": 10.0, "px_now": 100.0}})
    p = v3.plan_review(D, hold, book_figs({"H1": {}, **{s: {} for s in syms}}),
                       ranks_of(syms), keys, 5e5, 5e5 - 1000)
    bought = [o["symbol"] for o in p.orders if o["kind"] == "buy"]
    assert bought[:3] == ["C0", "C1", "C2"]                 # H1 + 3 = 4 metal names
    assert "C3" not in bought and "C4" not in bought
    assert bought[3:] == ["C5", "C6", "C7"]
    assert p.decision["slots_left_empty"] == 18 - 1 - 6
    assert p.decision["empty_reasons"] == "none_eligible;sector_cap"


def test_unclassified_is_one_sector():
    syms = [f"U{i}" for i in range(6)]
    p = v3.plan_review(D, hold_of({}).reindex(columns=["units", "px_now", "U0", "held_prev"]),
                       book_figs({s: {} for s in syms}), ranks_of(syms), pd.Series(dtype=object),
                       5e5, 5e5)
    assert len([o for o in p.orders if o["kind"] == "buy"]) == 4


def test_sector_keys_casefold_and_legacy_labels():
    sec = pd.DataFrame({"sector": ["Financial Services", "FINANCIAL SERVICES", "METALS"],
                        "label_scheme": ["current", "current", "legacy"]},
                       index=pd.Index(["A", "B", "C"], name="symbol"))
    k = v3.sector_keys(sec)
    assert k["A"] == k["B"] == "financial services" and k["C"] == v3.UNCLASSIFIED


def test_half_slot_minimum_stops_filling():
    syms = [f"C{i}" for i in range(5)]
    nav = 5e5
    cash = 2 * nav / 18 * 1.002 + 0.4 * nav / 18 * 1.002     # two full slots, then 0.4 slot
    p = v3.plan_review(D, hold_of({}).reindex(columns=["units", "px_now", "U0", "held_prev"]),
                       book_figs({s: {} for s in syms}), ranks_of(syms),
                       pd.Series({s: s for s in syms}), nav, cash)
    assert [o["symbol"] for o in p.orders] == ["C0", "C1"]
    assert p.decision["empty_reasons"] == "half_slot_minimum"


def test_exits_codes_order_and_e4_needs_the_previous_review():
    f = book_figs({"L": {"e1": True, "e5": True}, "G": {"down_both": True},
                   "G2": {"down_both": True}, "K": {}})
    hold = hold_of({"L": {"units": 1.0, "px_now": 10.0},
                    "G": {"units": 1.0, "px_now": 10.0, "held_prev": True},
                    "G2": {"units": 1.0, "px_now": 10.0, "held_prev": False},
                    "K": {"units": 1.0, "px_now": 10.0}})
    p = v3.plan_review(D, hold, f, ranks_of([]), pd.Series(dtype=object), 1e6, 0.0)
    rows = {r["symbol"]: r for r in p.holdings}
    assert rows["L"]["status"] == "exited" and rows["L"]["exit_codes"] == "E1;E5"
    assert rows["G"]["exit_codes"] == "E4"
    assert rows["G2"]["status"] == "kept"                   # bought at the last review
    full = {o["symbol"]: o for o in p.orders if o["full"]}
    assert set(full) == {"L", "G"} and full["L"]["reason"] == "E1"


def test_untested_inputs_are_listed_and_do_not_sell():
    f = book_figs({"X": {"pe": np.nan, "e1_known": False, "e2_known": False, "e3_on": True,
                         "e3_known": False, "down_known": False}})
    hold = hold_of({"X": {"units": 1.0, "px_now": 10.0, "held_prev": True}})
    p = v3.plan_review(D_DE, hold, f, ranks_of([]), pd.Series(dtype=object), 1e6, 0.0)
    r = p.holdings[0]
    assert r["status"] == "kept" and r["untested"] == "E1;E2;E3;E4;trim"
    assert p.decision["n_untested"] == 1


def test_owner_worked_example_valuation_trim_in_units():
    """Section 9.3, rows 1 to 6, with the 10% cap never binding."""
    def step(units, px, nav, pe, U0):
        f = book_figs({"H": {"pe": pe}})
        hold = hold_of({"H": {"units": units, "px_now": px, "U0": U0, "held_prev": True}})
        return v3.plan_review(D, hold, f, ranks_of([]), pd.Series(dtype=object), nav, 0.0)

    p = step(700, 100, 10_00_000, 89.25, np.nan)
    assert p.U0["H"] == 700 and p.holdings[0]["u_V"] == pytest.approx(630)
    assert p.orders[0]["units"] == pytest.approx(70) and p.orders[0]["kind"] == "valuation"
    held = 700 - p.orders[0]["units"]                     # what the fill leaves (9.4)
    p = step(held, 120, 10_20_000, 89.25, 700)
    assert p.orders == [] and p.U0["H"] == 700
    p = step(held, 140, 11_00_000, 102.0, 700)
    assert p.orders[0]["units"] == pytest.approx(210)
    assert p.holdings[0]["u_C"] == pytest.approx(0.10 * 11_00_000 / 140)
    p = step(420, 110, 11_00_000, 80.0, 700)
    assert p.orders == [] and np.isnan(p.U0["H"])          # cleared; never bought back
    p = step(420, 130, 11_20_000, 93.5, np.nan)
    assert p.U0["H"] == 420 and p.orders[0]["units"] == pytest.approx(84)
    p = step(336, 150, 11_20_000, 127.5, 420)
    assert p.orders[0]["full"] and p.orders[0]["reason"] == "V"
    assert p.holdings[0]["exit_codes"] == "V" and p.holdings[0]["status"] == "exited"


def test_unknown_pe_keeps_U0_and_skips_the_trim():
    f = book_figs({"H": {"pe": np.nan}})
    hold = hold_of({"H": {"units": 500, "px_now": 100, "U0": 700, "held_prev": True}})
    p = v3.plan_review(D, hold, f, ranks_of([]), pd.Series(dtype=object), 10_00_000, 0.0)
    assert p.U0["H"] == 700 and p.orders == []


def test_ten_percent_cap_binds_below_the_valuation_target():
    # weight 12%, P/E 89.25, no U0 yet: target 0.9 U0 is 10.8%, the cap 10%
    f = book_figs({"H": {"pe": 89.25}})
    hold = hold_of({"H": {"units": 1200, "px_now": 100.0}})
    p = v3.plan_review(D, hold, f, ranks_of([]), pd.Series(dtype=object), 10_00_000, 0.0)
    o = p.orders[0]
    assert o["kind"] == "cap" and o["units"] == pytest.approx(1200 - 1000)
    assert p.holdings[0]["status"] == "trimmed_cap"
    # and with no P/E problem at all, the cap alone
    f = book_figs({"H": {"pe": 30.0}})
    p = v3.plan_review(D, hold, f, ranks_of([]), pd.Series(dtype=object), 10_00_000, 0.0)
    assert p.orders[0]["kind"] == "cap" and np.isnan(p.holdings[0]["u_V"])


def test_review_order_budget_candidates_and_kept_slots():
    """Steps 8 to 11: a trimmed holding is kept (takes a slot), an exiting one
    is not; candidates leave out every stock held at D's open; the budget adds
    the expected sale proceeds net of cost."""
    syms = ["SOLD", "TRIM"] + [f"C{i:02d}" for i in range(20)]
    f = book_figs({s: {} for s in syms})
    f.loc["SOLD", "e1"] = True
    f.loc["TRIM", "pe"] = 30.0
    hold = hold_of({"SOLD": {"units": 100.0, "px_now": 50.0},
                    "TRIM": {"units": 2000.0, "px_now": 100.0}})
    nav, cash = 10_00_000, 9_00_000
    p = v3.plan_review(D, hold, f, ranks_of(syms), pd.Series({s: s for s in syms}), nav, cash)
    d = p.decision
    assert d["n_kept"] == 1 and d["empty_slots"] == 17
    assert d["budget_B"] == pytest.approx(cash + 100 * 50 * 0.998 + 1000 * 100 * 0.998)
    bought = [o["symbol"] for o in p.orders if o["kind"] == "buy"]
    assert "SOLD" not in bought and "TRIM" not in bought and len(bought) == 17


# ---------------------------------------------------------------- execution
def _panel(opens, traded=None, buyable=None, symbols=None):
    opens = np.asarray(opens, float)
    T, S = opens.shape
    dates = pd.bdate_range("2021-01-04", periods=T)
    traded = np.ones((T, S), bool) if traded is None else np.asarray(traded, bool)
    buyable = traded.copy() if buyable is None else np.asarray(buyable, bool) & traded
    open_ = np.where(traded, opens, np.nan)
    close = open_.copy()
    close[-1] = np.nan
    last = pd.DataFrame(close).ffill().to_numpy()
    run = np.zeros((T, S), dtype=np.int32)
    run[0] = np.where(traded[0], 0, 1)
    for t in range(1, T):
        run[t] = np.where(traded[t], 0, run[t - 1] + 1)
    syms = pd.Index(symbols or [f"S{i}" for i in range(S)])
    return engine.Panel(dates=dates, symbols=syms, open_=open_, close=close, last=last,
                        traded=traded, no_trade_run=run, div_unit=np.zeros((T, S)),
                        end=dates[-1], buyable=buyable)


def _review(panel, t, figs_rows, rank_syms):
    Dt = panel.dates[t]
    return Dt, v3.Review(D=Dt, figs=book_figs(figs_rows), ranks=ranks_of(rank_syms), funnel={})


def _sim(panel, reviews, keys=None):
    return v3.simulate_v3(panel, list(reviews), reviews,
                          keys if keys is not None else pd.Series(
                              {s: s for s in panel.symbols}),
                          is_end=panel.end, initial_capital=1_00_000)


def test_buys_fill_at_open_and_trim_is_sell_only_in_fixed_units():
    T = 12
    opens = np.full((T, 1), 100.0)
    opens[6:, 0] = 1000.0                                    # price x10 before review 2
    panel = _panel(opens, symbols=["A"])
    traded = panel.traded.copy()
    traded[7, 0] = False                                     # no trade at review 2's open
    panel = _panel(opens, traded=traded, symbols=["A"])
    D1, r1 = _review(panel, 0, {"A": {}}, ["A"])
    D2, r2 = _review(panel, 7, {"A": {}}, ["A"])
    res = _sim(panel, {D1: r1, D2: r2})
    buy = res.fills[(res.fills["kind"] == "buy") & (res.fills["event"] == "fill")]
    assert len(buy) == 1 and buy["value"].iloc[0] == pytest.approx(1_00_000 / 18)
    units = buy["units"].iloc[0]
    trim = res.fills[res.fills["kind"] == "cap"]
    assert list(trim["event"]) == ["fill"] and trim["side"].iloc[0] == "sell"
    assert trim["date"].iloc[0] == panel.dates[8].date()     # waited for a trade
    # the units were fixed at D from the last close (px_now), not refilled at the open
    nav_open = res.decisions["nav_open"].iloc[1]
    u_C = 0.10 * nav_open / 1000.0
    assert trim["units"].iloc[0] == pytest.approx(units - u_C)


def test_sells_fill_before_buys_and_buys_scale_pro_rata():
    T = 10
    opens = np.full((T, 3), 100.0)
    panel = _panel(opens, symbols=["A", "B", "C"])
    D1, r1 = _review(panel, 0, {s: {} for s in "ABC"}, ["A"])
    # at review 2, A exits; B and C are bought with the proceeds
    D2, r2 = _review(panel, 3, {"A": {"e1": True}, "B": {}, "C": {}}, ["B", "C"])
    res = _sim(panel, {D1: r1, D2: r2})
    f = res.fills[(res.fills["date"] == panel.dates[3].date()) & (res.fills["event"] == "fill")]
    assert list(f["side"]) == ["sell", "buy", "buy"]
    assert f["kind"].iloc[0] == "exit" and f["reason"].iloc[0] == "E1"


def test_buy_waits_for_an_eq_be_session_and_is_cancelled_after_five():
    T = 10
    opens = np.full((T, 1), 100.0)
    buyable = np.ones((T, 1), bool)
    buyable[0:6, 0] = False                                   # BZ only for 6 sessions
    panel = _panel(opens, buyable=buyable, symbols=["A"])
    D1, r1 = _review(panel, 0, {"A": {}}, ["A"])
    res = _sim(panel, {D1: r1})
    f = res.fills
    assert list(f["event"]) == ["cancel"] and f["kind"].iloc[0] == "buy"
    assert f["date"].iloc[0] == panel.dates[5].date()         # after the 5th session after D
    assert res.nav["nav"].iloc[-1] == pytest.approx(1_00_000)


def test_forced_exit_after_twenty_sessions_clears_the_holding():
    T = 30
    opens = np.full((T, 1), 100.0)
    traded = np.ones((T, 1), bool)
    traded[2:, 0] = False
    panel = _panel(opens, traded=traded, symbols=["A"])
    D1, r1 = _review(panel, 0, {"A": {}}, ["A"])
    res = _sim(panel, {D1: r1})
    fx = res.fills[res.fills["kind"] == "forced_exit"]
    assert len(fx) == 1 and fx["date"].iloc[0] == panel.dates[21].date()
    assert res.episodes["exit_reason"].iloc[0] == "no_trade_exit"


def test_e4_fires_only_at_the_second_review_after_purchase():
    T = 14
    panel = _panel(np.full((T, 1), 100.0), symbols=["A"])
    rows = {"A": {"down_both": True, "eligible": True}}
    D1, r1 = _review(panel, 0, {"A": {}}, ["A"])
    D2, r2 = _review(panel, 4, rows, [])
    D3, r3 = _review(panel, 8, rows, [])
    res = _sim(panel, {D1: r1, D2: r2, D3: r3})
    h = res.holdings.set_index("D")
    assert h.loc[D2, "status"] == "kept"                       # held at D2's open, not D1's
    assert h.loc[D3, "status"] == "exited" and h.loc[D3, "exit_codes"] == "E4"


def test_valuation_sale_clears_U0_and_the_name_can_return_later():
    T = 16
    panel = _panel(np.full((T, 1), 100.0), symbols=["A"])
    D1, r1 = _review(panel, 0, {"A": {}}, ["A"])
    D2, r2 = _review(panel, 4, {"A": {"pe": 90.0}}, [])
    D3, r3 = _review(panel, 8, {"A": {"pe": 130.0}}, [])
    D4, r4 = _review(panel, 12, {"A": {}}, ["A"])
    res = _sim(panel, {D1: r1, D2: r2, D3: r3, D4: r4})
    h = res.holdings
    assert list(h["status"]) == ["new", "trimmed_valuation", "exited", "new"]
    assert h["exit_codes"].iloc[2] == "V"
    assert np.isnan(h["U0"].iloc[3])

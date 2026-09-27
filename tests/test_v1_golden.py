"""Golden regression of the v1 baseline's headline figures.

Every headline figure in data/backtest/v1_final/baseline_report.json is
recomputed here from the run's own records, by code written for this test
(neither quantstats nor zen/portfolio/metrics.py is called), and must match
two references to 1e-9:

  1. the value stored in baseline_report.json, so the report still describes
     its own nav.csv and trades.csv;
  2. the v1 baseline value pinned below (commit d3eb964), so an edit that
     changes a result fails loudly even if the run and its report were
     regenerated together.

Inputs:
  nav.csv                 strategy and universe NAV at every mark (open of
                          15 Feb 2019, every close, open of 18 Sep 2026)
  trades.csv              every fill
  in_sample_end_open.csv  the value at the open of 15 Feb 2023. nav.csv holds
                          only closes between its two opens, so the held-back
                          sub-period, which starts at that open, needs this
                          file (v1 disclosure 37)
  data/zen.duckdb         capacity only: the 60-session median turnover
                          before each trade is in the prices table, not in the
                          run's files. That test is skipped without the database.

Definitions, as baseline_report.json states them:
  CAGR           calendar-day: (NAV at the end / NAV at the start) ^ (365.25 / days) - 1
  max drawdown   worst fall of NAV from its running peak, over every mark
  Sortino        mean of the mark-to-mark returns over the root mean square of
                 the negative ones taken over all returns, times sqrt(252);
                 zero risk-free rate (quantstats' definition)
  Calmar         quantstats' CAGR, (product of 1 + r) ^ (252 / number of
                 returns) - 1, over the absolute max drawdown
  turnover       every buy and sell's value / 2 / mean NAV over all marks /
                 calendar years (the initial build included)
  capacity       95th percentile (linear interpolation), over every buy and
                 sell, of |value| / the stock's median daily turnover over the
                 60 sessions before the trade date, untraded sessions as 0
  held-back CAGR calendar-day CAGR from the open of 15 Feb 2023 to the open of
                 18 Sep 2026. The report stores it rounded to 0.01 percentage
                 point, so it is checked against the report by rounding and
                 against the pinned exact value to 1e-9.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data" / "backtest" / "v1_final"
DB = ROOT / "data" / "zen.duckdb"
TOL = 1e-9

# The v1 baseline (commit d3eb964). Change these only with a new, reviewed baseline.
GOLDEN = {
    "marks": 1873,
    "fills": 452,
    "strategy_cagr": 0.30677147631337953,
    "universe_ew_cagr": 0.22029106529638498,
    "max_drawdown": -0.33559777608094643,
    "sortino": 1.9363344957714033,
    "calmar": 0.9367149608263851,
    "turnover_annual_incl_initial": 1.957688967486525,
    "capacity_p95_share_of_turnover": 0.011588713558527565,
    "held_back_strategy_cagr": 0.3431297617276783,
    "held_back_universe_ew_cagr": 0.20961084876814984,
}


@pytest.fixture(scope="module")
def run():
    nav = pd.read_csv(RUN / "nav.csv", parse_dates=["date"])
    trades = pd.read_csv(RUN / "trades.csv", parse_dates=["date"])
    report = json.loads((RUN / "baseline_report.json").read_text(encoding="utf-8"))
    iso = pd.read_csv(RUN / "in_sample_end_open.csv", parse_dates=["date"])
    # nav.csv must be the strategy clock: an open, every close in date order, an open
    assert list(nav["mark"].iloc[[0, -1]]) == ["open", "open"]
    assert (nav["mark"].iloc[1:-1] == "close").all()
    assert nav["date"].is_monotonic_increasing
    return {"nav": nav, "trades": trades, "report": report, "iso": iso}


def _years(d0, d1) -> float:
    return (pd.Timestamp(d1).normalize() - pd.Timestamp(d0).normalize()).days / 365.25


def _cagr(v0: float, v1: float, d0, d1) -> float:
    return float((v1 / v0) ** (1 / _years(d0, d1)) - 1)


def _returns(nav: pd.Series) -> pd.Series:
    return (nav / nav.shift(1) - 1).iloc[1:]


def _max_drawdown(nav: pd.Series) -> float:
    return float((nav / nav.cummax()).min() - 1)


def _check(name: str, got: float, reported: float) -> None:
    assert abs(got - reported) <= TOL, (name, "vs baseline_report.json", got, reported)
    assert abs(got - GOLDEN[name]) <= TOL, (name, "vs the pinned v1 value", got, GOLDEN[name])


def _a2(report, key) -> float:
    return report["a2_acceptance_quantities"][key]["value"]


# ---------------------------------------------------------------- the records themselves
def test_the_run_has_the_baseline_shape(run):
    nav, t = run["nav"], run["trades"]
    assert len(nav) == GOLDEN["marks"]
    assert int(t["side"].isin(["buy", "sell"]).sum()) == GOLDEN["fills"]
    rp = run["report"]["period"]
    assert (str(nav["date"].iloc[0].date()), str(nav["date"].iloc[-1].date())) == (
        rp["start"], rp["end"]) == ("2019-02-15", "2026-09-18")


# ---------------------------------------------------------------- headline
def test_cagr(run):
    nav = run["nav"]
    d0, d1 = nav["date"].iloc[0], nav["date"].iloc[-1]
    h = run["report"]["headline"]
    _check("strategy_cagr", _cagr(nav["strategy"].iloc[0], nav["strategy"].iloc[-1], d0, d1),
           h["strategy_cagr"]["value"])
    _check("universe_ew_cagr",
           _cagr(nav["universe_ew"].iloc[0], nav["universe_ew"].iloc[-1], d0, d1),
           h["universe_ew_cagr"]["value"])


def test_max_drawdown(run):
    _check("max_drawdown", _max_drawdown(run["nav"]["strategy"]),
           _a2(run["report"], "max_drawdown"))


def test_sortino(run):
    r = _returns(run["nav"]["strategy"])
    downside = np.sqrt((r[r < 0] ** 2).sum() / len(r))
    _check("sortino", float(r.mean() / downside * np.sqrt(252)), _a2(run["report"], "sortino"))


def test_calmar(run):
    nav = run["nav"]["strategy"]
    r = _returns(nav)
    cagr_252 = (1 + r).prod() ** (252 / len(r)) - 1
    _check("calmar", float(cagr_252 / abs(_max_drawdown(nav))), _a2(run["report"], "calmar"))


def test_turnover(run):
    nav, t = run["nav"], run["trades"]
    fills = t[t["side"].isin(["buy", "sell"])]
    yrs = _years(nav["date"].iloc[0], nav["date"].iloc[-1])
    got = float(fills["value"].abs().sum() / 2 / nav["strategy"].mean() / yrs)
    _check("turnover_annual_incl_initial", got,
           _a2(run["report"], "turnover_annual_incl_initial"))


def test_held_back_sub_period_cagr(run):
    nav, iso, rep = run["nav"], run["iso"], run["report"]
    assert len(iso) == 1 and iso["date"].iloc[0] == pd.Timestamp("2023-02-15")
    d0, d1 = iso["date"].iloc[0], nav["date"].iloc[-1]
    # The open value is recorded twice by the engine; the two records must agree.
    ro = pd.read_csv(RUN / "rebalance_open.csv", parse_dates=["date"]).set_index("date")
    assert abs(iso["strategy"].iloc[0] / ro.at[d0, "strategy_open"] - 1) <= 1e-12
    assert abs(iso["universe_ew"].iloc[0] / ro.at[d0, "universe_ew_open"] - 1) <= 1e-12
    ft = rep["final_test"]["value"]
    assert ft["clock"] == f"open of {d0.date()} to open of {d1.date()}"
    assert abs(ft["years"] - _years(d0, d1)) <= TOL
    for col, name, pct in (("strategy", "held_back_strategy_cagr", "strategy_cagr_pct"),
                           ("universe_ew", "held_back_universe_ew_cagr", "universe_ew_cagr_pct")):
        got = _cagr(iso[col].iloc[0], nav[col].iloc[-1], d0, d1)
        assert abs(got - GOLDEN[name]) <= TOL, (name, got, GOLDEN[name])
        # the report keeps two decimals of a percentage point
        assert round(got * 100, 2) == ft[pct], (name, got, ft[pct])


# ---------------------------------------------------------------- capacity (needs the prices table)
@pytest.mark.skipif(not DB.exists(), reason="data/zen.duckdb not present")
def test_capacity(run):
    from zen.universe import pit
    from zen.universe.identity import Identity

    t = run["trades"]
    fills = t[t["side"].isin(["buy", "sell"])].reset_index(drop=True)
    con = pit.connect(read_only=True)
    try:
        ids = Identity.build(con)
        cal = pit.sessions(con)
        stocks = set(fills["symbol"])
        tickers = sorted(set(ids.spells.loc[ids.spells["cid"].isin(stocks), "symbol"]) | stocks)
        lo = cal[cal.searchsorted(fills["date"].min()) - 60]
        con.register("_tickers", pd.DataFrame({"symbol": tickers}))
        px = con.execute(
            "SELECT p.symbol, p.isin_code, p.date, p.turnover FROM prices p "
            "JOIN _tickers USING (symbol) WHERE p.close > 0 AND p.date >= ? AND p.date < ?",
            [lo.date(), fills["date"].max().date()]).df()
        con.unregister("_tickers")
    finally:
        con.close()
    px["date"] = pd.to_datetime(px["date"])
    px["stock"] = ids.for_prices(px)
    # one figure per stock and session: the traded ticker's, where a rename overlaps
    day = px[px["stock"].isin(stocks)].groupby(["stock", "date"])["turnover"].max()
    share = []
    for r in fills.itertuples():
        i = cal.searchsorted(r.date)
        window = cal[i - 60:i]
        s = day.xs(r.symbol, level="stock") if r.symbol in day.index.get_level_values(0) \
            else pd.Series(dtype=float)
        med = float(np.median(s.reindex(window).fillna(0.0).to_numpy()))
        share.append(abs(r.value) / med if med > 0 else np.inf)
    share = np.asarray(share)
    assert len(share) == GOLDEN["fills"] and np.isfinite(share).all()
    _check("capacity_p95_share_of_turnover", float(np.quantile(share, 0.95)),
           _a2(run["report"], "capacity_p95_share_of_turnover"))

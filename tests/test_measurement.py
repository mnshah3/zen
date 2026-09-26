"""The measurement jobs, generic over a run folder (v2-spec A2).

jobs/libcheck_v1.py, jobs/verify_v1.py, jobs/stats_v1.py, jobs/attribution.py
and jobs/baseline_report.py are run on small synthetic run folders whose
answers are known, plus one read-only check against the real v1 re-run when
its outputs exist. No strategy is simulated and nothing is written outside
pytest's temporary folders.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
import quantstats as qs
import statsmodels.api as sm

from jobs import attribution as at
from jobs import baseline_report as br
from jobs import libcheck_v1 as lc
from jobs import stats_v1 as st
from jobs import verify_v1 as vf
from zen.portfolio import metrics

ROOT = Path(__file__).resolve().parents[1]
V1_FINAL = ROOT / "data" / "backtest" / "v1_final"


# ---------------------------------------------------------------- a synthetic run
def _walk(rng, n, drift, vol, start=100.0):
    return start * np.cumprod(1 + rng.normal(drift, vol, n))


def make_run(tmp_path: Path, seed: int = 1, split: str = "2019-07-01") -> Path:
    """nav.csv on the clock (open of the first date, closes, open of the last), with
    the portfolios, two indices and a no-overnight variant; the in-sample end open;
    rebalance opens; trades; metrics.json as the engine's metrics module computes it."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2019-02-15", "2019-12-31")
    n = len(days)
    cols = {"strategy": _walk(rng, n + 1, 0.001, 0.01, 5e5),
            "universe_ew": _walk(rng, n + 1, 0.0006, 0.01, 5e5),
            "nifty500": _walk(rng, n + 1, 0.0004, 0.008, 1.0),
            "momentum30": _walk(rng, n + 1, 0.0005, 0.01, 1.0)}
    cols["momentum30_no_overnight"] = cols["momentum30"] * 1.001
    rows = [{"date": days[0], "mark": "open"}] + \
           [{"date": d, "mark": "close"} for d in days[:-1]] + [{"date": days[-1], "mark": "open"}]
    nav = pd.DataFrame(rows)
    for k, v in cols.items():
        nav[k] = v
    for k in ("nifty500", "momentum30", "momentum30_no_overnight"):
        nav[k] = nav[k] / nav[k].iloc[0]
    run = tmp_path / "run"
    run.mkdir()
    nav.to_csv(run / "nav.csv", index=False)
    d = pd.Timestamp(split)
    prior = nav[(nav["date"] < d)].iloc[-1]
    # the open differs from the previous close, so open-to-open is distinguishable
    opens = {k: float(prior[k]) * 1.02 for k in cols}
    pd.DataFrame([{"date": d.date(), **opens}]).to_csv(run / "in_sample_end_open.csv", index=False)
    pd.DataFrame({"date": [days[0].date(), d.date()],
                  "strategy_open": [5e5, opens["strategy"]],
                  "universe_ew_open": [5e5, opens["universe_ew"]]}).to_csv(
        run / "rebalance_open.csv", index=False)
    trades = pd.DataFrame({"date": [days[0], days[0], d, d, d],
                           "symbol": ["A", "B", "A", "C", "B"],
                           "side": ["buy", "buy", "sell", "buy", "cancel"],
                           "units": [10, 10, 10, 10, 0], "price": [1e4, 1.5e4, 1.2e4, 2e4, np.nan],
                           "value": [1e5, 1.5e5, 1.2e5, 2e5, 0.0], "cost": [200, 300, 240, 400, 0],
                           "reason": ["rebalance"] * 4 + ["no trade within 5 sessions"]})
    trades.to_csv(run / "trades.csv", index=False)
    pd.DataFrame({"symbol": ["A", "B", "C"], "D": [days[0], days[0], d],
                  "median_turnover_60": [1e7, 2e7, 4e7]}).to_parquet(run / "ranks.parquet")
    f = lambda c: pd.DataFrame({"date": nav["date"], "mark": nav["mark"], "nav": nav[c]})
    m = metrics.summary(f("strategy"))
    m["turnover_annual_incl_initial"] = metrics.turnover(trades, f("strategy"), exclude_initial=False)
    m["vs"] = {c: {**metrics.summary(f(c)), **metrics.active(f("strategy"), f(c))}
               for c in cols if c != "strategy"}
    (run / "metrics.json").write_text(json.dumps(m, default=float))
    return run


# ---------------------------------------------------------------- libcheck: the clock
def test_load_clock_keeps_every_mark_in_order(tmp_path):
    run = make_run(tmp_path)
    clock = lc.load_clock(run)
    nav = pd.read_csv(run / "nav.csv")
    assert len(clock) == len(nav)
    assert clock.index.is_unique and clock.index.is_monotonic_increasing
    assert clock["mark"].iloc[0] == "open" and clock.index[0].hour == 9
    assert clock["mark"].iloc[1] == "close" and clock.index[1].date() == clock.index[0].date()
    assert clock["strategy"].iloc[0] == nav["strategy"].iloc[0]


def test_load_clock_refuses_duplicates_and_unknown_marks(tmp_path):
    run = make_run(tmp_path)
    nav = pd.read_csv(run / "nav.csv")
    pd.concat([nav, nav.iloc[[3]]]).to_csv(run / "nav.csv", index=False)
    with pytest.raises(ValueError, match="duplicated or out of order"):
        lc.load_clock(run)
    nav.assign(mark=["mid"] + list(nav["mark"].iloc[1:])).to_csv(run / "nav.csv", index=False)
    with pytest.raises(ValueError, match="marks other than"):
        lc.load_clock(run)


def test_index_columns_are_everything_but_the_portfolios(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    assert lc.index_columns(clock) == ["nifty500", "momentum30", "momentum30_no_overnight"]


def test_cagr_uses_calendar_days_between_mark_dates():
    s = pd.Series([1.0, 1.21], index=[pd.Timestamp("2020-01-01 09:15"),
                                      pd.Timestamp("2022-01-01 09:15")])
    yrs = 731 / 365.25
    assert lc.years_between(s.index[0], s.index[-1]) == yrs
    assert lc.cagr(s) == pytest.approx(1.21 ** (1 / yrs) - 1, rel=1e-12)


def test_monthly_returns_are_month_end_to_month_end(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    s = clock["strategy"]
    m = lc.monthly_returns(s)
    # the old date-indexed construction gives the same numbers
    old = s.copy()
    old.index = old.index.normalize()
    ref = old.groupby(old.index.to_period("M")).last().pct_change().dropna()
    assert list(m.index.astype(str)) == list(ref.index.astype(str))
    np.testing.assert_allclose(m.to_numpy(), ref.to_numpy(), rtol=1e-14)
    assert str(m.index[0]) == "2019-03"


# ---------------------------------------------------------------- libcheck: regression
def _iima(months):
    rng = np.random.default_rng(3)
    f = pd.DataFrame(rng.normal(0, 0.03, (len(months), 4)), index=months, columns=lc.FACTORS)
    f["RF"] = 0.005
    return f


def test_ols_nw_subtracts_rf_from_the_portfolio_only_and_recovers_alpha():
    months = pd.period_range("2015-01", periods=120, freq="M")
    f = _iima(months)
    rng = np.random.default_rng(4)
    r = f["RF"] + 0.004 + 0.9 * f["MF"] + 0.3 * f["SMB"] + rng.normal(0, 1e-4, len(f))
    d = lc.factor_frame(r, None, f)
    out = lc.ols_nw(d, lc.FACTORS)
    assert out["exact"]["alpha_monthly"] == pytest.approx(0.004, abs=2e-5)
    assert out["loadings"]["MF"] == pytest.approx(0.9, abs=0.01)
    assert out["exact"]["alpha_annual"] == pytest.approx((1 + out["exact"]["alpha_monthly"]) ** 12 - 1)
    # the same numbers as statsmodels called directly
    fit = sm.OLS(d["r"] - d["RF"], sm.add_constant(d[lc.FACTORS])).fit(
        cov_type="HAC", cov_kwds={"maxlags": 3})
    assert out["exact"]["alpha_t"] == pytest.approx(float(fit.tvalues["const"]), rel=1e-12)
    assert out["months"] == 120 and out["first"] == "2015-01"


def test_factor_frame_keeps_only_months_every_series_has():
    months = pd.period_range("2019-01", periods=12, freq="M")
    f = _iima(months)
    r = pd.Series(0.01, index=months[2:])
    q = pd.Series(0.0, index=months[:-3])
    d = lc.factor_frame(r, {"QUAL": q}, f)
    assert list(d.index) == list(months[2:-3])


def test_load_iima_divides_by_100(tmp_path):
    p = tmp_path / "iima.csv"
    p.write_text("Date,SMB,HML,WML,MF,RF\n2020-01,1.0,2.0,3.0,4.0,0.5\n")
    f = lc.load_iima(p)
    assert f.loc[pd.Period("2020-01", "M"), "MF"] == pytest.approx(0.04)
    assert f.loc[pd.Period("2020-01", "M"), "RF"] == pytest.approx(0.005)


def test_regression_on_the_real_iima_file_uses_its_months(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    out = lc.regression(clock["strategy"])
    assert out["first"] == "2019-03" and out["last"] == "2019-12" and out["months"] == 10


# ---------------------------------------------------------------- libcheck: quantstats
def test_practitioner_matches_the_engine_metrics_on_the_same_clock(tmp_path):
    run = make_run(tmp_path)
    clock = lc.load_clock(run)
    p = lc.practitioner(clock["strategy"])
    m = json.loads((run / "metrics.json").read_text())
    assert p["exact"]["cagr_calendar"] == pytest.approx(m["cagr"], rel=1e-12)
    assert p["exact"]["max_drawdown"] == pytest.approx(m["max_drawdown"], rel=1e-12)
    assert p["exact"]["n_returns"] == m["n_returns"]
    r = clock["strategy"].pct_change().dropna()
    assert p["exact"]["sortino"] == pytest.approx(float(qs.stats.sortino(r, periods=252)))
    assert p["exact"]["calmar"] == pytest.approx(
        float(qs.stats.cagr(r, periods=252)) / abs(p["exact"]["max_drawdown"]))


def test_practitioner_drawdown_counts_the_starting_value_as_a_peak():
    t = pd.DatetimeIndex(["2020-01-01 09:15", "2020-01-01 15:30", "2020-01-02 15:30",
                          "2020-01-03 09:15"])
    nav = pd.Series([100.0, 80.0, 90.0, 120.0], index=t)
    assert lc.practitioner(nav)["exact"]["max_drawdown"] == pytest.approx(-0.2)


def test_agreement_flags_a_difference():
    lib = {"exact": {"cagr_calendar": 0.1, "max_drawdown": -0.2, "volatility": 0.15, "sharpe": 1.0}}
    out = lc.agreement(lib, {"cagr": 0.1, "max_drawdown": -0.21, "volatility": 0.15})
    assert out["cagr"]["agree"] and not out["max_drawdown"]["agree"]
    assert "sharpe_rf0" not in out


# ---------------------------------------------------------------- libcheck: final test
def test_split_values_reads_the_open_and_checks_rebalance_open(tmp_path):
    run = make_run(tmp_path)
    d, vals = lc.split_values(run)
    assert d == pd.Timestamp("2019-07-01")
    assert set(vals.index) >= {"strategy", "universe_ew", "nifty500", "momentum30"}
    ro = pd.read_csv(run / "rebalance_open.csv")
    ro.loc[1, "strategy_open"] *= 1.01
    ro.to_csv(run / "rebalance_open.csv", index=False)
    with pytest.raises(ValueError, match="rebalance_open"):
        lc.split_values(run)
    (run / "in_sample_end_open.csv").unlink()
    assert lc.split_values(run) is None


def test_final_test_indices_run_open_to_open(tmp_path):
    run = make_run(tmp_path)
    clock = lc.load_clock(run)
    ft = lc.final_test(run, clock)
    opens = pd.read_csv(run / "in_sample_end_open.csv").iloc[0]
    yrs = lc.years_between(pd.Timestamp("2019-07-01"), clock.index[-1])
    for col in ("nifty500", "momentum30", "momentum30_no_overnight"):
        want = round(((clock[col].iloc[-1] / opens[col]) ** (1 / yrs) - 1) * 100, 2)
        assert ft["index_tri_cagr_pct"][col] == want
    # the old close-based start would give a different number
    prior_close = clock.loc[clock.index < pd.Timestamp("2019-07-01"), "nifty500"].iloc[-1]
    old = round(((clock["nifty500"].iloc[-1] / prior_close) ** (1 / yrs) - 1) * 100, 2)
    assert old != ft["index_tri_cagr_pct"]["nifty500"]
    want_s = round(((clock["strategy"].iloc[-1] / opens["strategy"]) ** (1 / yrs) - 1) * 100, 2)
    assert ft["strategy_cagr_pct"] == want_s
    assert ft["source"] == "in_sample_end_open.csv"


def test_leg_from_open_starts_at_the_recorded_open_and_includes_that_close(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    d = pd.Timestamp("2019-07-01")
    leg = lc.leg_from_open(clock, "strategy", d, 123.0)
    assert leg.iloc[0] == 123.0 and leg.index[0] == d + lc.OPEN_AT
    assert leg.index[1] == d + lc.CLOSE_AT


def test_final_test_without_the_open_file_computes_no_index_figures(tmp_path):
    run = make_run(tmp_path, split="2019-07-01")
    (run / "in_sample_end_open.csv").unlink()
    ro = pd.read_csv(run / "rebalance_open.csv")
    ro["date"] = ["2019-02-15", "2023-02-15"]          # the fallback looks for SPLIT
    ro.to_csv(run / "rebalance_open.csv", index=False)
    ft = lc.final_test(run, lc.load_clock(run))
    assert "index_tri_cagr_pct" not in ft


def test_margin_interval_point_is_the_growth_rate_gap():
    t = pd.date_range("2020-01-01", periods=300, freq="B") + lc.CLOSE_AT
    rng = np.random.default_rng(9)
    s = pd.Series(np.cumprod(1 + rng.normal(0.001, 0.01, 300)), index=t)
    e = pd.Series(np.cumprod(1 + rng.normal(0.0005, 0.01, 300)), index=t)
    out = lc.margin_interval(s, e, reps=200)
    r = pd.concat([s, e], axis=1).pct_change().dropna()
    py = len(r) / lc.years_between(t[0], t[-1])
    gap = (np.prod(1 + r[0]) ** (py / len(r)) - np.prod(1 + r[1]) ** (py / len(r)))
    assert out["point_pct"] == round(gap * 100, 2)
    assert out["lo90_pct"] <= out["point_pct"] <= out["hi90_pct"]


def test_run_libcheck_writes_every_section(tmp_path):
    rep = lc.run_libcheck(make_run(tmp_path), "t")
    assert rep["label"] == "t" and rep["clock"]["marks"] == len(pd.read_csv(tmp_path / "run" / "nav.csv"))
    assert all(v["agree"] for v in rep["agreement_with_metrics_json"].values())
    assert {"full_period", "regression_full_period", "final_test"} <= set(rep)


# ---------------------------------------------------------------- verify
def test_turnover_counts_every_trade_including_the_initial_build(tmp_path):
    run = make_run(tmp_path)
    r = vf.load_run(run)
    t = vf.turnover(r["trades"], r["strategy"])
    yrs = lc.years_between(r["strategy"].index[0], r["strategy"].index[-1])
    assert t == pytest.approx(5.7e5 / 2 / r["strategy"].mean() / yrs)
    m = json.loads((run / "metrics.json").read_text())
    assert t == pytest.approx(m["turnover_annual_incl_initial"], rel=1e-12)


def test_legacy_capacity_matches_only_decision_date_rows():
    trades = pd.DataFrame({"date": ["2020-01-01", "2020-01-03"], "symbol": ["A", "A"],
                           "value": [1e5, 1e5]})
    ranks = pd.DataFrame({"symbol": ["A"], "D": ["2020-01-01"], "median_turnover_60": [1e7]})
    out = vf.capacity(trades, ranks)
    assert out["trades"] == 2 and out["matched"] == 1 and out["p95_pct"] == pytest.approx(1.0)


def _prices_con():
    con = duckdb.connect()
    days = pd.bdate_range("2020-01-01", periods=10)
    rows = []
    for i, d in enumerate(days):
        rows.append(("A", "INE000A01", d, 10.0, 10.0, float(100 * (i + 1)), "EQ"))
        if i % 2 == 0:                        # B trades every other session
            rows.append(("B", "INE000B01", d, 5.0, 5.0, 50.0, "EQ"))
    rows.append(("A", "INE000A01", days[2], 10.0, 10.0, 1.0, "BE"))   # a second row, smaller
    rows.append(("C", "INE000C01", days[3], 0.0, 0.0, 999.0, "EQ"))  # close 0: not a price row
    df = pd.DataFrame(rows, columns=["symbol", "isin_code", "date", "open", "close", "turnover",
                                     "series"])
    con.register("df", df)
    con.execute("CREATE TABLE prices AS SELECT symbol, isin_code, CAST(date AS DATE) AS date, "
                "open, close, turnover, series FROM df")
    return con, days


def test_median_turnover_uses_sessions_strictly_before_with_zeros_for_untraded():
    con, days = _prices_con()
    pairs = pd.DataFrame({"symbol": ["A", "B", "A", "C"],
                          "date": [days[5], days[5], days[9], days[9]]})
    med = vf.median_turnover_at(con, pairs, ids=None, window=4)
    # A before days[5]: sessions 1..4 -> 200, 300, 400, 500 (the duplicate row keeps 300)
    assert med[0] == pytest.approx(350.0)
    # B before days[5]: sessions 1..4 -> 0, 50, 0, 50
    assert med[1] == pytest.approx(25.0)
    assert med[2] == pytest.approx(np.median([600, 700, 800, 900]))
    assert med[3] == 0.0                      # never a valid price row


def test_median_turnover_refuses_too_little_history():
    con, days = _prices_con()
    with pytest.raises(ValueError, match="fewer than"):
        vf.median_turnover_at(con, pd.DataFrame({"symbol": ["A"], "date": [days[2]]}), window=4)


def test_check_against_ranks_passes_on_equal_and_raises_on_different():
    trades = pd.DataFrame({"symbol": ["A", "B"], "date": pd.to_datetime(["2020-01-01", "2020-01-02"])})
    ranks = pd.DataFrame({"symbol": ["A"], "D": pd.to_datetime(["2020-01-01"]),
                          "median_turnover_60": [100.0]})
    assert vf.check_against_ranks(trades, np.array([100.0, 5.0]), ranks) == {"matched": 1,
                                                                              "worst_rel": 0.0}
    with pytest.raises(RuntimeError, match="differs from ranks"):
        vf.check_against_ranks(trades, np.array([101.0, 5.0]), ranks)


def test_capacity_all_trades_p95_and_zero_turnover_as_infinite():
    trades = pd.DataFrame({"side": ["buy"] * 20, "value": np.arange(1, 21, dtype=float),
                           "reason": ["rebalance"] * 20})
    med = np.full(20, 100.0)
    out = vf.capacity_all_trades(trades, med)
    assert out["p95"] == pytest.approx(np.quantile(np.arange(1, 21) / 100, 0.95))
    med[-2:] = 0.0                            # the two largest shares are now infinite
    out = vf.capacity_all_trades(trades, med)
    assert out["zero_turnover_trades"] == 2 and out["p95"] == float("inf")
    trades = pd.DataFrame({"side": ["buy"] * 100, "value": np.arange(1, 101, dtype=float),
                           "reason": ["rebalance"] * 100})
    med = np.full(100, 1000.0)
    med[0] = 0.0                              # one infinite share among a hundred
    out = vf.capacity_all_trades(trades, med)
    assert np.isinf(out["max_pct"]) and out["p95"] == pytest.approx(
        np.quantile(np.r_[np.arange(2, 101) / 1000, np.inf], 0.95))


def test_subperiods_run_open_to_open(tmp_path):
    run = make_run(tmp_path)
    r = vf.load_run(run)
    sub = vf.subperiods(run, r["strategy"], r["universe_ew"])
    opens = pd.read_csv(run / "in_sample_end_open.csv").iloc[0]
    s = r["strategy"]
    yrs = lc.years_between(pd.Timestamp("2019-07-01"), s.index[-1])
    assert sub["final_test"]["strategy_cagr_pct"] == round(
        ((s.iloc[-1] / opens["strategy"]) ** (1 / yrs) - 1) * 100, 2)
    yrs0 = lc.years_between(s.index[0], pd.Timestamp("2019-07-01"))
    assert sub["in_sample"]["strategy_cagr_pct"] == round(
        ((opens["strategy"] / s.iloc[0]) ** (1 / yrs0) - 1) * 100, 2)


def test_by_year_chains_from_the_previous_year_end():
    t = pd.DatetimeIndex(["2019-12-30 15:30", "2019-12-31 15:30", "2020-01-01 15:30",
                          "2020-12-31 15:30"])
    s = pd.Series([100.0, 110.0, 121.0, 132.0], index=t)
    y = vf.by_year(s, s)
    assert y[2019]["strategy_pct"] == 10.0 and y[2020]["strategy_pct"] == 20.0


def test_reproduce_refuses_a_run_the_engine_does_not_reproduce():
    class FakeEngine:
        def __init__(self, navs): self.navs = navs
        def run_strategy(self, *a, **k):
            return type("R", (), {"nav": pd.DataFrame({"nav": self.navs})})()
    s = pd.Series([1.0, 1.1, 1.2])
    _, worst = vf.reproduce(FakeEngine([1.0, 1.1, 1.2]), None, None, None, None, None, True, s)
    assert worst == 0.0
    with pytest.raises(RuntimeError, match="not the same machinery"):
        vf.reproduce(FakeEngine([1.0, 1.1, 1.21]), None, None, None, None, None, True, s)
    with pytest.raises(RuntimeError, match="marks"):
        vf.reproduce(FakeEngine([1.0, 1.1]), None, None, None, None, None, True, s)


def test_load_engine_refuses_a_config_that_is_not_v1s(tmp_path):
    r = vf.load_run(make_run(tmp_path))
    r["metrics"]["config"] = {"n": 12, "tranches": 3}
    r["metrics"]["decision_dates"] = ["2019-02-15"]
    with pytest.raises(ValueError, match="not a v1 engine config"):
        vf.load_engine(None, r)
    r["metrics"].pop("config")
    with pytest.raises(ValueError, match="cannot replay"):
        vf.load_engine(None, r)


def test_deflated_sharpe_three_ways_are_probabilities(tmp_path):
    r = vf.load_run(make_run(tmp_path))
    g = tmp_path / "grid.csv"
    pd.DataFrame({"sharpe_rf0": [0.5, 0.8, 1.1, 1.3]}).to_csv(g, index=False)
    d = vf.deflated(r["strategy"], g)
    for k in ("prob_null_sampling_variance", "prob_cross_trial_variance_36_grid",
              "prob_grid_trials_only"):
        assert 0.0 <= d[k] <= 1.0
    assert d["n_obs"] == len(r["strategy"]) - 1


# ---------------------------------------------------------------- stats
def test_stats_benchmark_is_the_run_s_own_column(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    b, src = st.benchmark(clock)
    assert b.index.equals(clock.index) and "nav.csv column nifty500" in src


def test_stats_build_on_a_run_folder(tmp_path):
    run = make_run(tmp_path)
    pd.DataFrame({"symbol": ["A"], "entry": ["2019-02-15"], "exit": ["2019-07-01"],
                  "entry_px": [1e4], "exit_reason": ["rebalance"], "max_close": [1.3e4],
                  "doubled": [False], "days_held": [136]}).to_csv(run / "episodes.csv", index=False)
    pd.DataFrame({"D": ["2019-02-15", "2019-02-15"], "symbol": ["A", "B"], "action": ["buy"] * 2,
                  "sector": ["X", None]}).to_csv(run / "holdings.csv", index=False)
    rep, dd = st.build(run, "t")
    assert rep["label"] == "t" and rep["period"]["start"] == "2019-02-15"
    m = json.loads((run / "metrics.json").read_text())
    assert rep["strategy"]["max_drawdown_pct"] == pytest.approx(m["max_drawdown"] * 100)
    assert rep["positions"]["closed_episodes"] == 1
    assert rep["positions"]["median_return_pct"] == pytest.approx((1.2e5 - 240) / (1e5 + 200) * 100 - 100)
    assert list(rep["monthly_pct"])[0] == "2019-03-31"
    assert all(len(x["start"]) >= 10 for x in rep["worst_drawdowns"])


# ---------------------------------------------------------------- attribution
def _qf(tmp_path, months, values, version="margin", complete=None):
    p = tmp_path / "qf.parquet"
    pd.DataFrame({"version": version, "month": [str(m) for m in months], "factor": values,
                  "complete": complete if complete is not None else [True] * len(months)}
                 ).to_parquet(p)
    return p


def test_quality_factor_keeps_complete_months_of_one_version(tmp_path):
    months = pd.period_range("2019-03", periods=4, freq="M")
    p = _qf(tmp_path, months, [0.01, 0.02, 0.03, 0.04], complete=[True, True, True, False])
    q = at.quality_factor("margin", p)
    assert list(q.index.astype(str)) == ["2019-03", "2019-04", "2019-05"]
    with pytest.raises(ValueError, match="no complete months"):
        at.quality_factor("roce", p)


def test_with_without_uses_identical_months_and_refuses_otherwise():
    months = pd.period_range("2015-01", periods=60, freq="M")
    f = _iima(months)
    rng = np.random.default_rng(5)
    q = pd.Series(rng.normal(0, 0.02, 60), index=months)
    r = f["RF"] + 0.003 + f["MF"] + 0.5 * q + rng.normal(0, 1e-3, 60)
    out = at.with_without(r, q[12:], f)
    assert out["months"] == 48 and out["first"] == "2016-01"
    assert out["without_quality"]["months"] == out["with_quality"]["months"] == 48
    assert out["with_quality"]["loadings"]["QUAL"] == pytest.approx(0.5, abs=0.05)
    assert out["with_quality"]["exact"]["alpha_monthly"] == pytest.approx(0.003, abs=5e-4)
    q_nan = q.copy()
    q_nan.iloc[20] = np.nan                   # the factor lacks a month the index still lists
    with pytest.raises(AssertionError, match="identical months"):
        at.with_without(r, q_nan, f)


def test_excess_equals_metrics_active(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    e = at.excess(clock, "nifty500")
    a, b = at.nav_frame(clock, "strategy"), at.nav_frame(clock, "nifty500")
    act = metrics.active(a, b)
    assert e["excess_cagr"] == act["cagr_diff"] and e["information_ratio"] == act["information_ratio"]
    assert e["n_returns"] == len(clock) - 1


def test_live_clock_starts_at_the_close_of_the_live_date(tmp_path):
    clock = lc.load_clock(make_run(tmp_path))
    assert at.live_clock(clock, None) is clock
    assert at.live_clock(clock, "2020-10-12") is None      # live after the run ended
    assert at.live_clock(clock, "2018-01-01") is clock
    lc_ = at.live_clock(clock, "2019-06-03")
    assert lc_.index[0] == pd.Timestamp("2019-06-03") + lc.CLOSE_AT
    assert lc_.index[-1] == clock.index[-1]
    with pytest.raises(ValueError, match="not a session close"):
        at.live_clock(clock, "2019-06-01")     # a Saturday


def test_check_live_basis_reads_nse_prints(tmp_path):
    p = tmp_path / "ep.csv"
    rows = ['"IndexName","Date","Open","High","Low","Close"']
    for col, name in at.ENDPOINT_NAMES.items():
        o = "-" if at.LIVE_START[col][0] else "100.0"
        rows.append(f'"{name}","15 Feb 2019","{o}","-","-","101.0"')
    p.write_text("\n".join(rows) + "\n")
    out = at.check_live_basis(pd.Timestamp("2019-02-15"), p)
    assert out["momentum30"]["open_printed_at_clock_start"] is False
    assert out["quality30"]["open_printed_at_clock_start"] is True
    p.write_text("\n".join(rows).replace('"NIFTY200 MOMENTUM 30","15 Feb 2019","-"',
                                         '"NIFTY200 MOMENTUM 30","15 Feb 2019","99.0"') + "\n")
    with pytest.raises(AssertionError, match="momentum30"):
        at.check_live_basis(pd.Timestamp("2019-02-15"), p)


def test_benchmarks_check_against_metrics_json_and_add_live_parts(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    clock = lc.load_clock(run)
    m = json.loads((run / "metrics.json").read_text())
    out = at.benchmarks(clock, m)
    assert out["nifty500"]["whole_period"]["agrees_with_metrics_json"]
    assert "live_part" not in out["nifty500"]
    assert out["momentum30"]["live_part"]["note"] == "not live within this run"
    monkeypatch.setitem(at.LIVE_START, "momentum30",
                        ("2019-06-03", "first date NSE printed an open (fallback)", "test"))
    out = at.benchmarks(clock, m)
    lp = out["momentum30_no_overnight"]["live_part"]
    assert lp["from"] == "2019-06-03 close" and lp["basis"].startswith("first date")
    assert lp["excess_cagr"] == at.excess(at.live_clock(clock, "2019-06-03"),
                                          "momentum30_no_overnight")["excess_cagr"]
    m["vs"]["nifty500"]["cagr_diff"] += 1e-6
    with pytest.raises(AssertionError, match="nifty500 excess_cagr"):
        at.benchmarks(clock, m)


# ---------------------------------------------------------------- baseline report
def _docs():
    ex = {"max_drawdown": -0.30, "sortino": 1.9, "calmar": 0.9, "cagr_calendar": 0.3}
    reg = {"months": 82, "first": "2019-03", "last": "2025-12",
           "exact": {"alpha_annual": 0.05, "alpha_t": 1.2}}
    return {"libcheck": {"full_period": {"strategy": {"exact": ex}},
                         "regression_full_period": {"strategy": reg}},
            "verify": {"turnover_annual_incl_initial": 1.9,
                       "attribution": {"strategy": {"alpha_t": 1.17}},
                       "capacity": {"p95_pct": 0.9, "matched": 400},
                       "capacity_all_trades": {"p95": 0.011, "trades": 450, "definition": "d",
                                               "check_against_ranks": {"matched": 400}}},
            "metrics": {"max_drawdown": -0.30, "turnover_annual_incl_initial": 1.9}}


def test_pick_reads_nested_keys_and_names_what_is_missing():
    d = {"a": {"b": {"c": 3}}}
    assert br.pick(d, "a.b.c") == 3
    with pytest.raises(KeyError, match="a.x.c"):
        br.pick(d, "a.x.c")


def test_a2_quantities_carry_value_source_and_bar():
    q = br.a2_quantities(_docs())
    assert q["max_drawdown"]["value"] == -0.30
    assert q["max_drawdown"]["bar_for_v2"]["v2_max_drawdown_must_be_at_least"] == pytest.approx(-0.25)
    assert q["iima_alpha_t"]["value"] == 1.2
    assert q["iima_alpha_t"]["hand_rolled_with_small_sample_correction"]["value"] == 1.17
    assert q["capacity_p95_share_of_turnover"]["value"] == 0.011
    assert q["turnover_annual_incl_initial"]["source"] == "verify.json: turnover_annual_incl_initial"
    for v in q.values():
        assert v["definition"] and v["source"] and "bar_for_v2" in v


def test_load_refuses_a_run_without_its_measurements(tmp_path):
    with pytest.raises(FileNotFoundError, match="run the measurement job"):
        br.load(make_run(tmp_path))


@pytest.mark.skipif(not (V1_FINAL / "baseline_report.json").exists(),
                    reason="the v1 re-run's measurement outputs are not present")
def test_real_baseline_report_matches_its_sources():
    """Read-only: every A2 figure in the published report is the number its job wrote."""
    rep = json.loads((V1_FINAL / "baseline_report.json").read_text(encoding="utf-8"))
    for name, fig in rep["a2_acceptance_quantities"].items():
        file, key = fig["source"].split(": ")
        assert br.pick(json.loads((V1_FINAL / file).read_text()), key) == fig["value"], name
    a = rep["a2_acceptance_quantities"]
    assert a["max_drawdown"]["value"] == pytest.approx(a["max_drawdown"]["check_metrics_json"])
    assert a["turnover_annual_incl_initial"]["value"] == pytest.approx(
        a["turnover_annual_incl_initial"]["check_metrics_json"], rel=1e-12)
    md = (V1_FINAL / "baseline_report.md").read_text(encoding="utf-8")
    assert f"{a['max_drawdown']['value'] * 100:.2f}%" in md

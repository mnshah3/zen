"""Is the v1 result skill, or is it luck wearing a suit?

The held-back test said 29.4% a year against 20.4% for the same universe
equally weighted. That is one number from one path, and a single number can
flatter a strategy in at least four ways this file tries to catch.

  LUCK. Ten stocks is a small sample. Some random picks from a universe that
  returned 20% a year will return 35%. The monkey test draws hundreds of random
  ten-stock portfolios through the SAME machinery -- same dates, same costs,
  same buffer and sector cap -- and reports where the strategy lands among
  them. If it sits near the middle, the ranking adds nothing and the result is
  the universe plus noise.

  A FACTOR IN DISGUISE. Small companies and past winners both did well in India
  over this period. If the strategy is just those two exposures, it is not a
  discovery, it is a repackaging, and it can be bought more cheaply. Monthly
  returns are regressed on IIM Ahmedabad's published Indian factors (market,
  size, value, momentum). What matters is whether alpha survives.

  ONE GOOD YEAR. 2021 returned 95%. A strategy can look excellent because of a
  single window. Returns are broken out by year and by half.

  A SIZE THAT DOES NOT EXIST. A position that is 10% of a stock's daily volume
  cannot be bought at the printed price. Trade value is compared with the
  stock's own turnover on the day.

Corrected on 2026-09-23 after an independent audit: the factor regression had
subtracted the risk-free rate twice, the final test was measured from the wrong
mark, calendar years skipped their first session, the random portfolios were
compared at a much higher turnover than the strategy, and the deflated Sharpe
was reported as one number when it depends heavily on modelling choices.

    python -m jobs.verify_v1                 # 500 monkeys
    python -m jobs.verify_v1 --monkeys 2000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from math import erf, sqrt
from math import log as _ln
from pathlib import Path

import numpy as np
import pandas as pd

from zen.portfolio import engine
from zen.universe import pit
from zen.validation import trials

log = logging.getLogger(__name__)

OUT = Path("data/backtest/verify")
RANKS = Path("data/backtest/v1_holdout/ranks.parquet")
END = pd.Timestamp("2026-09-18")
IIMA = Path("data/external/iima/2025-12_FourFactors_and_Market_Returns_Monthly_"
            "SurvivorshipBiasAdjusted.csv")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF, good to about 1e-9 and dependency free."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = sqrt(-2 * _ln(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = sqrt(-2 * _ln(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def cagr(nav: pd.Series) -> float:
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    return (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1


def load(con):
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    decisions = [d for d in pit.decision_dates(cal, last=(END.year, END.month)) if d < END]
    ranks = pd.read_parquet(RANKS)
    ranks["D"] = pd.to_datetime(ranks["D"])
    ranks = ranks[ranks["D"].isin(decisions)]
    static = pit.StaticLabels.load(con)
    panel = engine.build_panel(con, sorted(ranks["symbol"].unique()), decisions[0], END,
                               is_end, run_final_test=True, ids=static.ids)
    return cal, is_end, decisions, ranks, panel


def monkeys(panel, ranks, decisions, cfg, is_end, n_draws: int, mode: str = "persistent",
            seed: int = 20260922):
    """Random ten-stock portfolios through the same machinery.

    Only the ORDER of the ranking is randomised. Every other rule -- the
    universe, the buffer, the sector cap, costs, dividends, the forced exit --
    is the strategy's own.

    Two ways to randomise, because the first one flatters the strategy:

      fresh       a new random order every quarter. A holding then almost never
                  stays inside the hold band, so the random book churns far more
                  than the strategy and pays far more in costs. Part of the
                  strategy's apparent lead over these is just lower turnover.
      persistent  one random score per stock for the whole run, ranked afresh
                  within each date's universe. Churn then comes mostly from
                  stocks entering and leaving the universe, as it largely does
                  for the strategy, so the comparison is about selection rather
                  than trading.

    Returns (cagr per draw, annual one-way turnover per draw).
    """
    rng = np.random.default_rng(seed)
    byd = {D: g.set_index("symbol") for D, g in ranks.groupby("D")}
    universe = sorted(ranks["symbol"].unique())
    out, turn = [], []
    for i in range(n_draws):
        if mode == "persistent":
            score = pd.Series(rng.random(len(universe)), index=universe)
        shuffled = {}
        for D, g in byd.items():
            r = g.copy()
            if mode == "persistent":
                r["rank"] = score.reindex(r.index).rank(method="first").astype(int).values
            else:
                r["rank"] = rng.permutation(np.arange(1, len(r) + 1))
            shuffled[D] = r

        def choose(D, held, _s=shuffled):
            return engine.select_top(_s[pd.Timestamp(D)], held, cfg), {}

        res = engine.simulate(panel, engine.rebalance_dates(decisions, cfg.rebalance),
                              choose, cfg, is_end, run_final_test=True, log_trades=True)
        nav = res.nav.set_index("date")["nav"]
        out.append(cagr(nav))
        turn.append(turnover(res.trades, nav))
        if (i + 1) % 100 == 0:
            log.info("  %d/%d monkeys (%s)", i + 1, n_draws, mode)
    return np.array(out), np.array(turn)


def turnover(trades: pd.DataFrame, nav: pd.Series) -> float:
    """Annual one-way turnover: half of everything bought and sold, per year,
    over the average value of the book."""
    if trades.empty or "value" not in trades:
        return 0.0
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    return float(trades["value"].abs().sum() / 2 / nav.mean() / yrs)


def attribution(strategy_nav: pd.Series, ew_nav: pd.Series) -> dict:
    """Regress monthly excess returns on IIMA's published Indian factors.

    If the strategy is only size and momentum in disguise, the factor loadings
    absorb the return and alpha collapses. Newey-West errors with 3 lags,
    because monthly portfolio returns are not independent.
    """
    if not IIMA.exists():
        return {"error": "IIMA factor file missing; see data/external/SOURCES.md"}
    f = pd.read_csv(IIMA)
    f["Date"] = pd.to_datetime(f["Date"], format="%Y-%m").dt.to_period("M")
    for c in ["SMB", "HML", "WML", "MF", "RF"]:
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f = f.set_index("Date")[["SMB", "HML", "WML", "MF", "RF"]] / 100.0

    out = {}
    for name, nav in (("strategy", strategy_nav), ("universe_ew", ew_nav)):
        m = nav.resample("ME").last().pct_change().dropna()
        m.index = m.index.to_period("M")
        d = f.join(m.rename("r"), how="inner").dropna()
        if len(d) < 24:
            out[name] = {"error": f"only {len(d)} overlapping months"}
            continue
        y = (d["r"] - d["RF"]).values
        # IIMA's MF column is ALREADY the market return minus the risk-free
        # rate. Subtracting RF again, as an earlier version of this file did,
        # understated the market's return by RF every month, pushed that return
        # into the intercept and roughly doubled the reported alpha: 9.5% at
        # t=1.96 where the correct figure is 4.7% at t=0.99.
        X = np.column_stack([np.ones(len(d)), d["MF"], d["SMB"], d["HML"], d["WML"]])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        n, k = X.shape
        xtx_inv = np.linalg.inv(X.T @ X)
        L = 3                                        # Newey-West lags
        S = (resid[:, None] * X).T @ (resid[:, None] * X)
        for l in range(1, L + 1):
            u = (resid[:, None] * X)
            G = u[l:].T @ u[:-l]
            S += (1 - l / (L + 1)) * (G + G.T)
        cov = xtx_inv @ S @ xtx_inv * n / (n - k)
        se = np.sqrt(np.diag(cov))
        names = ["alpha", "market", "size_SMB", "value_HML", "momentum_WML"]
        out[name] = {
            "months": int(n),
            "first_month": str(d.index[0]), "last_month": str(d.index[-1]),
            "alpha_monthly_pct": float(beta[0] * 100),
            "alpha_annual_pct": float(((1 + beta[0]) ** 12 - 1) * 100),
            "alpha_t": float(beta[0] / se[0]),
            "r_squared": float(1 - resid.var() / y.var()),
            "loadings": {names[i]: round(float(beta[i]), 3) for i in range(1, 5)},
            "loading_t": {names[i]: round(float(beta[i] / se[i]), 2) for i in range(1, 5)},
        }
    return out


def capacity(trades: pd.DataFrame, ranks: pd.DataFrame) -> dict:
    """Could these trades have been done at the printed price?"""
    if trades.empty:
        return {}
    t = trades.copy()
    t["D"] = pd.to_datetime(t["date"] if "date" in t else t["D"])
    value_col = next((c for c in ("value", "traded_value", "amount") if c in t), None)
    if value_col is None:
        qty = next((c for c in ("shares", "qty", "units") if c in t), None)
        px = next((c for c in ("price", "fill_price", "open") if c in t), None)
        if not (qty and px):
            return {"error": f"cannot find trade value in {list(t.columns)}"}
        t["_v"] = t[qty].abs() * t[px]
        value_col = "_v"
    liq = ranks[["symbol", "D", "median_turnover_60"]].copy()
    liq["D"] = pd.to_datetime(liq["D"])
    j = t.merge(liq, on=["symbol", "D"], how="left")
    j["share_of_turnover"] = j[value_col].abs() / j["median_turnover_60"]
    ok = j["share_of_turnover"].dropna()
    return {"trades": int(len(j)), "median_pct_of_daily_turnover": round(float(ok.median() * 100), 3),
            "p95_pct": round(float(ok.quantile(0.95) * 100), 3),
            "max_pct": round(float(ok.max() * 100), 3),
            "trades_over_5pct": int((ok > 0.05).sum()),
            "note": "at the backtest's Rs 5 lakh. Ten times the capital multiplies these by ten."}


def main(argv=None) -> int:
    global RANKS, OUT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--monkeys", type=int, default=500)
    ap.add_argument("--run", default="data/backtest/v1_corrected",
                    help="backtest output folder whose ranks.parquet to use")
    ap.add_argument("--out", default="data/backtest/verify")
    args = ap.parse_args(argv)
    RANKS, OUT = Path(args.run) / "ranks.parquet", Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)

    con = pit.connect()
    cal, is_end, decisions, ranks, panel = load(con)
    cfg = engine.Config(n=10, buffer_mult=2, rebalance="quarterly", sector_cap=3,
                        cost=0.002, initial_capital=500_000)
    log.info("decision dates %s..%s (%d)", decisions[0].date(), decisions[-1].date(), len(decisions))

    strat = engine.run_strategy(panel, ranks, decisions, cfg, is_end, run_final_test=True)
    ew = engine.run_universe_ew(panel, ranks, decisions, cfg, is_end, run_final_test=True)
    s_nav = strat.nav.set_index("date")["nav"]
    e_nav = ew.nav.set_index("date")["nav"]
    s_cagr, e_cagr = cagr(s_nav), cagr(e_nav)
    log.info("strategy %.1f%% a year, universe EW %.1f%%", s_cagr * 100, e_cagr * 100)

    monkey = {"strategy_cagr_pct": round(s_cagr * 100, 2),
              "strategy_turnover": round(turnover(strat.trades, s_nav), 2)}
    for mode in ("persistent", "fresh"):
        log.info("monkey test (%s): %d random ten-stock portfolios", mode, args.monkeys)
        mk, mt = monkeys(panel, ranks, decisions, cfg, is_end, args.monkeys, mode=mode)
        monkey[mode] = {"draws": int(len(mk)),
                        "random_median_pct": round(float(np.median(mk)) * 100, 2),
                        "random_p5_pct": round(float(np.percentile(mk, 5)) * 100, 2),
                        "random_p95_pct": round(float(np.percentile(mk, 95)) * 100, 2),
                        "random_best_pct": round(float(mk.max()) * 100, 2),
                        "random_median_turnover": round(float(np.median(mt)), 2),
                        "strategy_percentile": round(float((mk < s_cagr).mean() * 100), 1),
                        "beaten_by_n_random": int((mk >= s_cagr).sum())}
        pd.DataFrame({"cagr": mk, "turnover": mt}).to_csv(OUT / f"monkey_{mode}.csv", index=False)

    log.info("factor attribution against IIMA")
    attr = attribution(s_nav, e_nav)

    # Deflated Sharpe three ways. The answer depends on which variance of trial
    # Sharpes is used and how many trials are counted, and none of the choices
    # is obviously right here, so the spread is what gets reported.
    daily = s_nav.pct_change().dropna()
    n_trials = trials.lifetime()
    sr = float(daily.mean() / daily.std())
    g1, g2 = float(daily.skew()), float(daily.kurt()) + 3.0
    grid = pd.read_csv("data/backtest/v1/grid.csv")
    cross_var = float((grid["sharpe_rf0"] / np.sqrt(252)).var())
    dsr = {"sharpe_annual": sr * np.sqrt(252), "n_obs": int(len(daily)), "n_trials": n_trials,
           "prob_null_sampling_variance": trials.deflated_sharpe(sr, n_trials, len(daily), g1, g2),
           "prob_cross_trial_variance_36_grid": trials.deflated_sharpe(
               sr, n_trials, len(daily), g1, g2, var_trial_sharpe=cross_var),
           "prob_grid_trials_only": trials.deflated_sharpe(sr, len(grid), len(daily), g1, g2),
           "note": "probability of genuine skill after the search; the spread between these "
                   "is the honest answer, not any single one"}

    # Sub-periods on the specification's own clock: open of the first decision
    # date to open of 15 Feb 2023, then open of 15 Feb 2023 to open of the end
    # date. The engine records the value at every rebalance open for this.
    split = pd.Timestamp("2023-02-15")
    s_open, e_open = strat.rebalance_open, ew.rebalance_open

    def seg(v0, v1, t0, t1):
        return (v1 / v0) ** (365.25 / (t1 - t0).days) - 1

    sub = {}
    for label, t0, v0s, v0e, t1, v1s, v1e in [
            ("in_sample_2019_2023", s_nav.index[0], s_nav.iloc[0], e_nav.iloc[0],
             split, s_open[split], e_open[split]),
            ("final_test_2023_2026", split, s_open[split], e_open[split],
             s_nav.index[-1], s_nav.iloc[-1], e_nav.iloc[-1])]:
        a_, b_ = seg(v0s, v1s, t0, t1), seg(v0e, v1e, t0, t1)
        sub[label] = {"strategy_cagr_pct": round(a_ * 100, 2),
                      "universe_ew_cagr_pct": round(b_ * 100, 2),
                      "diff_pct": round((a_ - b_) * 100, 2),
                      "from_open": str(t0.date()), "to_open": str(t1.date())}

    # Calendar years from the previous year-end value, not from the first mark
    # of the year, which skipped the first session of every year.
    yearly = {}
    s_y = s_nav.groupby(s_nav.index.year).last()
    e_y = e_nav.groupby(e_nav.index.year).last()
    prev_s, prev_e = s_nav.iloc[0], e_nav.iloc[0]
    for y in s_y.index:
        yearly[int(y)] = {"strategy_pct": round((s_y[y] / prev_s - 1) * 100, 1),
                          "universe_ew_pct": round((e_y[y] / prev_e - 1) * 100, 1)}
        prev_s, prev_e = s_y[y], e_y[y]

    cap = capacity(strat.trades, ranks)

    report = {"period": {"start": str(s_nav.index[0].date()), "end": str(s_nav.index[-1].date())},
              "headline": {"strategy_cagr_pct": round(s_cagr * 100, 2),
                           "universe_ew_cagr_pct": round(e_cagr * 100, 2)},
              "monkey_test": monkey, "attribution": attr, "deflated_sharpe": dsr,
              "subperiods": sub, "by_year": yearly, "capacity": cap}
    (OUT / "verify.json").write_text(json.dumps(report, indent=2, default=float))

    print("\n=== MONKEY TEST ===")
    print(f"  strategy {monkey['strategy_cagr_pct']}%/yr, turnover {monkey['strategy_turnover']}x/yr")
    for mode in ("persistent", "fresh"):
        m = monkey[mode]
        print(f"  {mode:10}: random median {m['random_median_pct']}% (turnover "
              f"{m['random_median_turnover']}x), 95th {m['random_p95_pct']}%, best "
              f"{m['random_best_pct']}%  ->  strategy at percentile {m['strategy_percentile']}, "
              f"beaten by {m['beaten_by_n_random']}/{m['draws']}")
    print("\n=== FACTOR ATTRIBUTION (IIMA four factors) ===")
    for k, v in attr.items():
        if "error" in v:
            print(f"  {k}: {v['error']}"); continue
        print(f"  {k}: alpha {v['alpha_annual_pct']:.1f}%/yr  t={v['alpha_t']:.2f}  "
              f"R2={v['r_squared']:.2f}  loadings {v['loadings']}")
    print("\n=== DEFLATED SHARPE ===")
    print(f"  Sharpe {dsr['sharpe_annual']:.2f} over {dsr['n_obs']} days, {dsr['n_trials']} lifetime trials")
    print(f"  probability of skill: {dsr['prob_null_sampling_variance']:.3f} (null sampling variance), "
          f"{dsr['prob_cross_trial_variance_36_grid']:.3f} (cross-trial variance, 36-variant grid), "
          f"{dsr['prob_grid_trials_only']:.3f} (counting only the 36 grid trials)")
    print("\n=== SUBPERIODS ===")
    for k, v in sub.items():
        print(f"  {k}: {v['strategy_cagr_pct']}% vs EW {v['universe_ew_cagr_pct']}% "
              f"({v['diff_pct']:+}pp)")
    print("\n=== CAPACITY ===")
    print(" ", cap)
    print(f"\nwritten to {OUT}/verify.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

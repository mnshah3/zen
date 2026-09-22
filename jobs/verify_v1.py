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

Also fixes the deflated Sharpe ratio. `trials.deflated_sharpe` was found by
audit to return roughly zero whatever it is given, because it compares an
annualised Sharpe with a per-period spread. The version here follows Bailey and
Lopez de Prado (2014) with everything in per-period units.

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


def deflated_sharpe(returns: pd.Series, n_trials: int, var_trial_sr: float | None = None) -> dict:
    """Bailey and Lopez de Prado (2014), in PER-PERIOD units throughout.

    The expected maximum Sharpe under the null of no skill grows with how many
    strategies were tried. A Sharpe that would be impressive after one attempt
    is ordinary after two hundred.
    """
    r = returns.dropna().values
    n = len(r)
    sr = r.mean() / r.std(ddof=1)
    g1 = float(pd.Series(r).skew())
    g2 = float(pd.Series(r).kurt()) + 3.0            # pandas kurt is excess
    if var_trial_sr is None:                          # spread of trial Sharpes
        var_trial_sr = (1 + 0.5 * sr ** 2) / n        # asymptotic variance of SR
    euler = 0.5772156649015329
    t = max(int(n_trials), 2)
    sr0 = sqrt(var_trial_sr) * ((1 - euler) * _norm_ppf(1 - 1.0 / t)
                                + euler * _norm_ppf(1 - 1.0 / (t * np.e)))
    denom = sqrt(max(1e-12, 1 - g1 * sr + (g2 - 1) / 4 * sr ** 2))
    z = (sr - sr0) * sqrt(n - 1) / denom
    return {"sharpe_per_period": sr, "sharpe_annual": sr * sqrt(252),
            "expected_max_sharpe_under_null": sr0,
            "expected_max_sharpe_annual": sr0 * sqrt(252),
            "skew": g1, "kurtosis": g2, "n_obs": n, "n_trials": t,
            "deflated_sharpe_prob": _norm_cdf(z)}


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


def monkeys(panel, ranks, decisions, cfg, is_end, n_draws: int, seed: int = 20260922):
    """Random ten-stock portfolios through the same machinery.

    Only the ORDER of the ranking is randomised. Every other rule -- the
    universe, the buffer, the sector cap, costs, dividends, the forced exit --
    is the strategy's own, so the difference measured is the ranking and
    nothing else.
    """
    rng = np.random.default_rng(seed)
    byd = {D: g.set_index("symbol") for D, g in ranks.groupby("D")}
    out = []
    for i in range(n_draws):
        shuffled = {}
        for D, g in byd.items():
            r = g.copy()
            r["rank"] = rng.permutation(np.arange(1, len(r) + 1))
            shuffled[D] = r
        def choose(D, held, _s=shuffled):
            return engine.select_top(_s[pd.Timestamp(D)], held, cfg), {}
        res = engine.simulate(panel, engine.rebalance_dates(decisions, cfg.rebalance),
                              choose, cfg, is_end, run_final_test=True, log_trades=False)
        nav = res.nav.set_index("date")["nav"]
        out.append(cagr(nav))
        if (i + 1) % 50 == 0:
            log.info("  %d/%d monkeys", i + 1, n_draws)
    return np.array(out)


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
        X = np.column_stack([np.ones(len(d)), d["MF"] - d["RF"], d["SMB"], d["HML"], d["WML"]])
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--monkeys", type=int, default=500)
    args = ap.parse_args(argv)
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

    log.info("monkey test: %d random ten-stock portfolios", args.monkeys)
    mk = monkeys(panel, ranks, decisions, cfg, is_end, args.monkeys)
    pct = float((mk < s_cagr).mean() * 100)
    monkey = {"draws": int(len(mk)), "strategy_cagr_pct": round(s_cagr * 100, 2),
              "random_median_pct": round(float(np.median(mk)) * 100, 2),
              "random_p5_pct": round(float(np.percentile(mk, 5)) * 100, 2),
              "random_p95_pct": round(float(np.percentile(mk, 95)) * 100, 2),
              "random_best_pct": round(float(mk.max()) * 100, 2),
              "strategy_percentile": round(pct, 1),
              "beaten_by_n_random": int((mk >= s_cagr).sum())}
    pd.Series(mk).to_csv(OUT / "monkey_cagrs.csv", index=False, header=["cagr"])

    log.info("factor attribution against IIMA")
    attr = attribution(s_nav, e_nav)

    daily = s_nav.pct_change().dropna()
    n_trials = sum(1 for _ in open("state/trials.jsonl", encoding="utf-8"))
    dsr = deflated_sharpe(daily, n_trials)

    sub = {}
    for label, lo, hi in [("in_sample_2019_2023", "2019-02-15", "2023-02-15"),
                          ("final_test_2023_2026", "2023-02-15", "2026-09-18")]:
        a, b = s_nav.loc[lo:hi], e_nav.loc[lo:hi]
        sub[label] = {"strategy_cagr_pct": round(cagr(a) * 100, 1),
                      "universe_ew_cagr_pct": round(cagr(b) * 100, 1),
                      "diff_pct": round((cagr(a) - cagr(b)) * 100, 1)}
    yearly = {}
    for y, g in s_nav.groupby(s_nav.index.year):
        e = e_nav.loc[g.index[0]:g.index[-1]]
        yearly[int(y)] = {"strategy_pct": round((g.iloc[-1] / g.iloc[0] - 1) * 100, 1),
                          "universe_ew_pct": round((e.iloc[-1] / e.iloc[0] - 1) * 100, 1)}

    cap = capacity(strat.trades, ranks)

    report = {"period": {"start": str(s_nav.index[0].date()), "end": str(s_nav.index[-1].date())},
              "headline": {"strategy_cagr_pct": round(s_cagr * 100, 2),
                           "universe_ew_cagr_pct": round(e_cagr * 100, 2)},
              "monkey_test": monkey, "attribution": attr, "deflated_sharpe": dsr,
              "subperiods": sub, "by_year": yearly, "capacity": cap}
    (OUT / "verify.json").write_text(json.dumps(report, indent=2, default=float))

    print("\n=== MONKEY TEST ===")
    print(f"  strategy {monkey['strategy_cagr_pct']}%/yr vs random median "
          f"{monkey['random_median_pct']}% (5th {monkey['random_p5_pct']}, "
          f"95th {monkey['random_p95_pct']}, best {monkey['random_best_pct']})")
    print(f"  percentile {monkey['strategy_percentile']}; beaten by "
          f"{monkey['beaten_by_n_random']} of {monkey['draws']} random portfolios")
    print("\n=== FACTOR ATTRIBUTION (IIMA four factors) ===")
    for k, v in attr.items():
        if "error" in v:
            print(f"  {k}: {v['error']}"); continue
        print(f"  {k}: alpha {v['alpha_annual_pct']:.1f}%/yr  t={v['alpha_t']:.2f}  "
              f"R2={v['r_squared']:.2f}  loadings {v['loadings']}")
    print("\n=== DEFLATED SHARPE ===")
    print(f"  Sharpe {dsr['sharpe_annual']:.2f} vs expected best under the null "
          f"{dsr['expected_max_sharpe_annual']:.2f} after {dsr['n_trials']} trials"
          f"  ->  probability of skill {dsr['deflated_sharpe_prob']:.3f}")
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

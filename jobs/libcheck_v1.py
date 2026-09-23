"""Recompute every published v1 statistic with standard libraries.

Every statistical error the second audit found was in code written by hand for
this project: a regression that subtracted the risk-free rate twice, a Sortino
ratio with the wrong denominator, a deflated Sharpe that could only return 0
or 1. Widely used libraries implement these the way the literature defines
them and are exercised by far more people than this project ever will be. So
the published numbers are recomputed here with them, and the project's own
figures are only trusted where the two agree.

  statsmodels   OLS with Newey-West (HAC) standard errors
  quantstats    CAGR, Sortino, Calmar, drawdown, as practitioners define them
  arch          stationary bootstrap (Politis and Romano 1994) for the interval
                on the final-test margin, which suits autocorrelated returns
                better than fixed blocks

    python -m jobs.libcheck_v1 --run data/backtest/v1_holdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import quantstats as qs
import statsmodels.api as sm
from arch.bootstrap import StationaryBootstrap

IIMA = Path("data/external/iima/2025-12_FourFactors_and_Market_Returns_Monthly_"
            "SurvivorshipBiasAdjusted.csv")
TRI = Path("data/external/nifty_tri.parquet")
SPLIT = pd.Timestamp("2023-02-15")


def load_nav(run: Path) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    nav = pd.read_csv(run / "nav.csv", parse_dates=["date"])
    # One value per date: the close where there is one, the open otherwise
    # (the first and last dates are open marks).
    nav = nav.sort_values(["date", "mark"]).drop_duplicates("date", keep="first")
    nav = nav.set_index("date")
    opens = pd.read_csv(run / "rebalance_open.csv", parse_dates=["date"]).set_index("date") \
        if (run / "rebalance_open.csv").exists() else pd.DataFrame()
    return nav["strategy"].dropna(), nav["universe_ew"].dropna(), opens


def regression(nav: pd.Series) -> dict:
    """Four-factor regression on IIMA's published Indian factors.

    IIMA's MF column is already the market return minus the risk-free rate, so
    it goes in as it is. Only the portfolio's return has RF taken off.
    """
    f = pd.read_csv(IIMA)
    f["Date"] = pd.to_datetime(f["Date"], format="%Y-%m").dt.to_period("M")
    f = f.set_index("Date")[["MF", "SMB", "HML", "WML", "RF"]].apply(pd.to_numeric, errors="coerce") / 100
    m = nav.resample("ME").last().pct_change().dropna()
    m.index = m.index.to_period("M")
    d = f.join(m.rename("r"), how="inner").dropna()
    y = d["r"] - d["RF"]
    X = sm.add_constant(d[["MF", "SMB", "HML", "WML"]])
    fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
    a = fit.params["const"]
    return {"months": int(len(d)), "first": str(d.index[0]), "last": str(d.index[-1]),
            "alpha_annual_pct": round(((1 + a) ** 12 - 1) * 100, 2),
            "alpha_t": round(float(fit.tvalues["const"]), 2),
            "r_squared": round(float(fit.rsquared), 3),
            "loadings": {k: round(float(fit.params[k]), 3) for k in ["MF", "SMB", "HML", "WML"]},
            "loading_t": {k: round(float(fit.tvalues[k]), 2) for k in ["MF", "SMB", "HML", "WML"]}}


def practitioner(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    # quantstats counts returns and divides by 252 rather than using the
    # calendar, and it drops the opening value, so its CAGR runs higher than a
    # calendar-day CAGR on NSE's ~246 sessions a year. Both are reported.
    return {"cagr_calendar_pct": round(((nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
            "cagr_quantstats_pct": round(qs.stats.cagr(r, periods=252) * 100, 2),
            "sharpe": round(qs.stats.sharpe(r, periods=252), 3),
            "sortino": round(qs.stats.sortino(r, periods=252), 3),
            "calmar": round(qs.stats.calmar(r), 3),
            "max_drawdown_pct": round(qs.stats.max_drawdown(r) * 100, 2),
            "volatility_pct": round(qs.stats.volatility(r, periods=252) * 100, 2)}


def margin_interval(s: pd.Series, e: pd.Series, reps: int = 5000, seed: int = 7) -> dict:
    """90% interval for the margin in annual growth rate over equal weight.

    The margin quoted in the README is a difference of compound annual growth
    rates, so the interval is for exactly that: each bootstrap draw rebuilds
    both return paths from the SAME resampled days and compounds them. An
    earlier version bootstrapped the annualised mean daily difference, which is
    a different statistic (10.1 points where the growth-rate margin is 12.0),
    and annualised with 252 sessions where NSE averaged about 246 a year.
    """
    j = pd.concat([s.rename("s"), e.rename("e")], axis=1).dropna()
    yrs = (j.index[-1] - j.index[0]).days / 365.25
    r = j.pct_change().dropna()
    per_year = len(r) / yrs

    def cagr_gap(rs, re_):
        return (np.prod(1 + rs) ** (per_year / len(rs)) - np.prod(1 + re_) ** (per_year / len(re_)))

    bs = StationaryBootstrap(21, r["s"].values, r["e"].values, seed=seed)
    draws = np.array([cagr_gap(d[0][0], d[0][1]) for d in bs.bootstrap(reps)])
    return {"point_pct": round(cagr_gap(r["s"].values, r["e"].values) * 100, 2),
            "lo90_pct": round(float(np.percentile(draws, 5)) * 100, 2),
            "hi90_pct": round(float(np.percentile(draws, 95)) * 100, 2),
            "share_of_draws_le_zero": round(float((draws <= 0).mean()), 3),
            "sessions_per_year": round(per_year, 1),
            "method": "stationary bootstrap of paired daily returns, mean block 21 "
                      "sessions, 5000 draws, statistic = difference in compound annual growth"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="data/backtest/v1_holdout")
    args = ap.parse_args(argv)
    run = Path(args.run)
    s, e, opens = load_nav(run)

    out = {"run": str(run),
           "full_period": {"strategy": practitioner(s), "universe_ew": practitioner(e)},
           "regression_full_period": {"strategy": regression(s), "universe_ew": regression(e)}}

    # Final test on the specification's clock: open of 15 Feb 2023 onward.
    if not opens.empty and SPLIT in opens.index:
        s_ft = pd.concat([pd.Series({SPLIT - pd.Timedelta(hours=1): opens.at[SPLIT, "strategy_open"]}),
                          s.loc[s.index > SPLIT]])
        e_ft = pd.concat([pd.Series({SPLIT - pd.Timedelta(hours=1): opens.at[SPLIT, "universe_ew_open"]}),
                          e.loc[e.index > SPLIT]])
        clock = "open of 2023-02-15 to open of the end date"
    else:
        s_ft, e_ft = s.loc[SPLIT:], e.loc[SPLIT:]
        clock = "close of 2023-02-15 (no rebalance_open.csv in this run)"
    yrs = (s_ft.index[-1] - s_ft.index[0]).days / 365.25
    cg = lambda v: round(((v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1) * 100, 2)
    out["final_test"] = {"clock": clock, "strategy_cagr_pct": cg(s_ft),
                         "universe_ew_cagr_pct": cg(e_ft),
                         "margin_interval": margin_interval(s_ft, e_ft)}
    if TRI.exists():
        t = pd.read_parquet(TRI)
        for name in ("NIFTY 500", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250"):
            x = t[t.index_name == name].set_index("date")["tri"].sort_index()
            # Index TRI has closes only: the last close before each open mark.
            x0 = x.loc[:SPLIT - pd.Timedelta(days=1)].iloc[-1]
            x1 = x.loc[:s_ft.index[-1] - pd.Timedelta(days=1)].iloc[-1]
            out["final_test"][f"{name.lower().replace(' ', '_')}_tri_cagr_pct"] = \
                round(((x1 / x0) ** (1 / yrs) - 1) * 100, 2)

    print(json.dumps(out, indent=2))
    (run / "libcheck.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

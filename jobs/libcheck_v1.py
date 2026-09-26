"""Recompute every published statistic of a backtest run with standard libraries.

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

Generic over a run folder (jobs/backtest_v1.py's layout, or any run that writes
nav.csv with date, mark, strategy, universe_ew and benchmark columns), so v1
and v2 are measured by the same code (v2-spec A2):

    python -m jobs.libcheck_v1 --run data/backtest/v1_final --label v1

Writes <run>/libcheck.json (or --out).

Clock. Every mark of nav.csv is used, in order: the open of the first decision
date, every close, the open of the end date (v1 Clarification 16). An earlier
version kept one value per date, which dropped the opening mark and started
every library figure at the first close. Marks are given unique timestamps
(09:15 for an open, 15:30 for a close) only so that pandas and quantstats see
an ordered index; no figure depends on the time of day.

Held-back sub-period (v1 disclosure 37). From the value at the open of the
in-sample end date to the open of the end date, for the strategy, the
equal-weight benchmark and every index column, all read from
in_sample_end_open.csv (the value of every nav.csv column at that open). An
earlier version measured the indices from the close of the session before,
which is not the specification's clock.
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

ROOT = Path(__file__).resolve().parents[1]
IIMA = ROOT / "data" / "external" / "iima" / \
    "2025-12_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted.csv"
SPLIT = pd.Timestamp("2023-02-15")        # used only when a run records no in-sample end
OPEN_AT = pd.Timedelta(hours=9, minutes=15)
CLOSE_AT = pd.Timedelta(hours=15, minutes=30)
FACTORS = ["MF", "SMB", "HML", "WML"]
NW_LAGS = 3                               # Newey-West lags for monthly returns
PORTFOLIOS = ("strategy", "universe_ew")
AGREE_TOL = 1e-9


# ---------------------------------------------------------------- the clock
def load_clock(run: Path) -> pd.DataFrame:
    """nav.csv with one row per mark, in clock order, on a unique timestamp index.

    Columns are nav.csv's own (mark, strategy, universe_ew, the benchmarks).
    Refuses a file whose marks are not open/close or whose rows are not a clock.
    """
    nav = pd.read_csv(Path(run) / "nav.csv", parse_dates=["date"])
    if not nav["mark"].isin(["open", "close"]).all():
        raise ValueError(f"{run}/nav.csv: marks other than open/close")
    at = pd.to_timedelta(np.where(nav["mark"] == "open", OPEN_AT.value, CLOSE_AT.value))
    idx = pd.DatetimeIndex(nav["date"].to_numpy() + at.to_numpy(), name="t")
    if not idx.is_unique or not idx.is_monotonic_increasing:
        raise ValueError(f"{run}/nav.csv: marks are duplicated or out of order")
    return nav.drop(columns=["date"]).set_index(idx)


def index_columns(clock: pd.DataFrame) -> list[str]:
    """Every benchmark index column (everything but the mark and the two portfolios)."""
    return [c for c in clock.columns if c not in ("mark", *PORTFOLIOS)]


def years_between(t0: pd.Timestamp, t1: pd.Timestamp) -> float:
    """Calendar years between two marks' dates (365.25 days), as the engine counts them."""
    return (pd.Timestamp(t1).normalize() - pd.Timestamp(t0).normalize()).days / 365.25


def cagr(v: pd.Series) -> float:
    return float((v.iloc[-1] / v.iloc[0]) ** (1 / years_between(v.index[0], v.index[-1])) - 1)


def monthly_returns(nav: pd.Series) -> pd.Series:
    """Calendar-month returns from the last value of each month, as the IIMA regression
    has always built them: the first month is the first full month after the start,
    and a partial last month is included (the factor files decide which months count)."""
    m = nav.resample("ME").last().pct_change().dropna()
    m.index = m.index.to_period("M")
    return m


# ---------------------------------------------------------------- IIMA factors
def load_iima(path: Path = IIMA) -> pd.DataFrame:
    """IIMA's monthly MF, SMB, HML, WML and RF as fractions, indexed by month.

    MF is already the market return minus the risk-free rate."""
    f = pd.read_csv(path)
    f["Date"] = pd.to_datetime(f["Date"], format="%Y-%m").dt.to_period("M")
    return f.set_index("Date")[FACTORS + ["RF"]].apply(pd.to_numeric, errors="coerce") / 100


def factor_frame(monthly: pd.Series, extra: dict[str, pd.Series] | None = None,
                 iima: pd.DataFrame | None = None) -> pd.DataFrame:
    """Monthly returns (column r) joined to IIMA and any extra factor: the months all have."""
    d = (load_iima() if iima is None else iima).join(monthly.rename("r"), how="inner")
    for name, s in (extra or {}).items():
        d = d.join(s.rename(name), how="inner")
    return d.dropna()


def ols_nw(d: pd.DataFrame, factors: list[str]) -> dict:
    """OLS of r - RF on the factors with Newey-West (HAC, 3 lags) errors, statsmodels.

    Only the portfolio's return has RF taken off: IIMA's MF is already excess."""
    y = d["r"] - d["RF"]
    X = sm.add_constant(d[factors])
    fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": NW_LAGS})
    a = float(fit.params["const"])
    return {"months": int(len(d)), "first": str(d.index[0]), "last": str(d.index[-1]),
            "alpha_annual_pct": round(((1 + a) ** 12 - 1) * 100, 2),
            "alpha_t": round(float(fit.tvalues["const"]), 2),
            "r_squared": round(float(fit.rsquared), 3),
            "loadings": {k: round(float(fit.params[k]), 3) for k in factors},
            "loading_t": {k: round(float(fit.tvalues[k]), 2) for k in factors},
            "exact": {"alpha_monthly": a, "alpha_annual": (1 + a) ** 12 - 1,
                      "alpha_t": float(fit.tvalues["const"]),
                      "alpha_p": float(fit.pvalues["const"]),
                      "r_squared": float(fit.rsquared)},
            "method": f"statsmodels OLS, HAC (Newey-West, Bartlett) with {NW_LAGS} lags; "
                      "alpha annualised as (1 + monthly alpha)^12 - 1"}


def regression(nav: pd.Series) -> dict:
    """Four-factor regression on IIMA's published Indian factors."""
    return ols_nw(factor_frame(monthly_returns(nav)), FACTORS)


# ---------------------------------------------------------------- quantstats
def practitioner(nav: pd.Series) -> dict:
    """quantstats' figures from the returns between consecutive marks of the clock."""
    r = nav.pct_change().dropna()
    # quantstats counts returns and divides by 252 rather than using the
    # calendar, so its CAGR runs higher than a calendar-day CAGR on NSE's ~246
    # sessions a year. Both are reported. Its Calmar uses its own CAGR.
    ex = {"cagr_calendar": cagr(nav),
          "cagr_quantstats": float(qs.stats.cagr(r, periods=252)),
          "sharpe": float(qs.stats.sharpe(r, periods=252)),
          "sortino": float(qs.stats.sortino(r, periods=252)),
          "calmar": float(qs.stats.calmar(r)),
          "max_drawdown": float(qs.stats.max_drawdown(r)),
          "volatility": float(qs.stats.volatility(r, periods=252)),
          "n_returns": int(len(r))}
    return {"cagr_calendar_pct": round(ex["cagr_calendar"] * 100, 2),
            "cagr_quantstats_pct": round(ex["cagr_quantstats"] * 100, 2),
            "sharpe": round(ex["sharpe"], 3),
            "sortino": round(ex["sortino"], 3),
            "calmar": round(ex["calmar"], 3),
            "max_drawdown_pct": round(ex["max_drawdown"] * 100, 2),
            "volatility_pct": round(ex["volatility"] * 100, 2),
            "exact": ex}


def agreement(lib: dict, metrics: dict) -> dict:
    """The library figures against the run's own metrics.json (same clock, same NAV)."""
    pairs = {"cagr": ("cagr_calendar", "cagr"), "max_drawdown": ("max_drawdown", "max_drawdown"),
             "volatility": ("volatility", "volatility"), "sharpe_rf0": ("sharpe", "sharpe_rf0")}
    out = {}
    for name, (lk, mk) in pairs.items():
        if mk not in metrics:
            continue
        d = float(lib["exact"][lk] - metrics[mk])
        out[name] = {"library": lib["exact"][lk], "project": metrics[mk], "difference": d,
                     "agree": bool(abs(d) <= AGREE_TOL)}
    return out


# ---------------------------------------------------------------- final test
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
    yrs = years_between(j.index[0], j.index[-1])
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


def split_values(run: Path) -> tuple[pd.Timestamp, pd.Series] | None:
    """(in-sample end date, every nav.csv column's value at its open), from
    in_sample_end_open.csv; None when the run did not record it.

    Cross-checked against rebalance_open.csv, which the engine records separately."""
    p = Path(run) / "in_sample_end_open.csv"
    if not p.exists():
        return None
    row = pd.read_csv(p, parse_dates=["date"])
    if len(row) != 1:
        raise ValueError(f"{p}: expected one row, found {len(row)}")
    d = pd.Timestamp(row["date"].iloc[0])
    vals = row.drop(columns=["date"]).iloc[0].astype(float)
    ro = Path(run) / "rebalance_open.csv"
    if ro.exists():
        r = pd.read_csv(ro, parse_dates=["date"]).set_index("date")
        if d in r.index:
            for col, rcol in (("strategy", "strategy_open"), ("universe_ew", "universe_ew_open")):
                if col in vals and abs(vals[col] / r.at[d, rcol] - 1) > 1e-12:
                    raise ValueError(f"{col} at the open of {d.date()}: in_sample_end_open.csv "
                                     f"{vals[col]} vs rebalance_open.csv {r.at[d, rcol]}")
    return d, vals


def leg_from_open(clock: pd.DataFrame, col: str, d: pd.Timestamp, v0: float) -> pd.Series:
    """A column from its value at the open of d to the end of the clock."""
    t0 = pd.Timestamp(d) + OPEN_AT
    return pd.concat([pd.Series({t0: float(v0)}), clock.loc[clock.index > t0, col].astype(float)])


def final_test(run: Path, clock: pd.DataFrame) -> dict:
    """Held-back sub-period: open of the in-sample end to the open of the end date."""
    sv = split_values(run)
    last = clock.index[-1]
    if sv is not None:
        d, vals = sv
        src = "in_sample_end_open.csv"
    else:
        ro = Path(run) / "rebalance_open.csv"
        d = SPLIT
        if ro.exists():
            r = pd.read_csv(ro, parse_dates=["date"]).set_index("date")
            if SPLIT not in r.index:
                return {"note": f"no value at the open of {SPLIT.date()} in this run"}
            vals = pd.Series({"strategy": r.at[SPLIT, "strategy_open"],
                              "universe_ew": r.at[SPLIT, "universe_ew_open"]})
            src = "rebalance_open.csv (no in_sample_end_open.csv: index figures not computed)"
        else:
            return {"note": "no open value at the in-sample end recorded in this run"}
    if clock["mark"].iloc[-1] != "open" or last.normalize() <= d:
        return {"note": f"the run does not extend past the open of {d.date()}"}
    s_ft = leg_from_open(clock, "strategy", d, vals["strategy"])
    e_ft = leg_from_open(clock, "universe_ew", d, vals["universe_ew"])
    yrs = years_between(d, last)
    out = {"clock": f"open of {d.date()} to open of {last.date()}", "source": src,
           "years": yrs,
           "strategy_cagr_pct": round(cagr(s_ft) * 100, 2),
           "universe_ew_cagr_pct": round(cagr(e_ft) * 100, 2),
           "margin_interval": margin_interval(s_ft, e_ft)}
    if sv is not None:
        idx = {}
        for col in index_columns(clock):
            if col not in vals or pd.isna(vals[col]):
                raise ValueError(f"in_sample_end_open.csv has no value for {col}")
            idx[col] = round(((clock[col].iloc[-1] / vals[col]) ** (1 / yrs) - 1) * 100, 2)
        out["index_tri_cagr_pct"] = idx
        out["index_clock"] = ("TRI at the open of the in-sample end (in_sample_end_open.csv) "
                              "to TRI at the open of the end date (nav.csv's last mark); "
                              "*_no_overnight: no overnight move where NSE printed no open")
    return out


# ---------------------------------------------------------------- main
def run_libcheck(run: Path, label: str) -> dict:
    run = Path(run)
    clock = load_clock(run)
    s, e = clock["strategy"].dropna(), clock["universe_ew"].dropna()
    out = {"run": str(run), "label": label,
           "clock": {"first": str(clock.index[0]), "last": str(clock.index[-1]),
                     "marks": int(len(clock))},
           "full_period": {"strategy": practitioner(s), "universe_ew": practitioner(e)},
           "regression_full_period": {"strategy": regression(s), "universe_ew": regression(e)}}
    mp = run / "metrics.json"
    if mp.exists():
        out["agreement_with_metrics_json"] = agreement(out["full_period"]["strategy"],
                                                       json.loads(mp.read_text()))
    out["final_test"] = final_test(run, clock)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="data/backtest/v1_final", help="backtest output folder")
    ap.add_argument("--label", default=None, help="name for this run (default: folder name)")
    ap.add_argument("--out", default=None, help="output file (default <run>/libcheck.json)")
    args = ap.parse_args(argv)
    run = Path(args.run)
    out = run_libcheck(run, args.label or run.name)
    print(json.dumps(out, indent=2))
    (Path(args.out) if args.out else run / "libcheck.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

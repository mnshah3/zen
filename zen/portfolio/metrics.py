"""Performance metrics for strategy v1 (spec: "Reported metrics").

CAGR, volatility, Sharpe, information ratio and tracking error against each
benchmark, maximum drawdown, calendar-year returns, turnover, average holding
period, holdings that doubled while held, and a block-bootstrap confidence
interval for the annualised return difference against a benchmark.

Conventions (stated once, used everywhere):
  - A NAV frame has columns date, mark ('open'|'close'), nav. Returns are
    point-to-point between consecutive marks, so the first return runs from
    the open of the first decision date to its close, and the last from the
    close before the end date to the open of the end date.
  - Annualisation: 252 periods a year for volatility, Sharpe, TE and IR; CAGR
    uses calendar days (365.25) between the first and last mark.
  - Sharpe uses a zero risk-free rate: the archive holds no Indian T-bill
    series, and the spec does not name one. It is therefore a return/volatility
    ratio and overstates a true Sharpe by roughly rf/vol.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PERIODS = 252


def returns(nav: pd.DataFrame) -> np.ndarray:
    v = nav["nav"].to_numpy(dtype=float)
    return v[1:] / v[:-1] - 1


def cagr(nav: pd.DataFrame) -> float:
    v = nav["nav"].to_numpy(dtype=float)
    days = (nav["date"].iloc[-1] - nav["date"].iloc[0]).days
    return float((v[-1] / v[0]) ** (365.25 / days) - 1) if days > 0 else np.nan


def vol(r: np.ndarray) -> float:
    return float(np.std(r, ddof=1) * np.sqrt(PERIODS))


def sharpe(r: np.ndarray) -> float:
    sd = np.std(r, ddof=1)
    return float(np.mean(r) / sd * np.sqrt(PERIODS)) if sd > 0 else np.nan


def max_drawdown(nav: pd.DataFrame) -> float:
    v = nav["nav"].to_numpy(dtype=float)
    peak = np.maximum.accumulate(v)
    return float((v / peak - 1).min())


def align(a: pd.DataFrame, b: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Returns of two NAV frames over their common (date, mark) points."""
    m = a.merge(b, on=["date", "mark"], suffixes=("_a", "_b"))
    va, vb = m["nav_a"].to_numpy(float), m["nav_b"].to_numpy(float)
    return va[1:] / va[:-1] - 1, vb[1:] / vb[:-1] - 1


def active(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    ra, rb = align(a, b)
    d = ra - rb
    te = float(np.std(d, ddof=1) * np.sqrt(PERIODS))
    ir = float(np.mean(d) * PERIODS / te) if te > 0 else np.nan
    return {"tracking_error": te, "information_ratio": ir,
            "cagr_diff": cagr(a) - cagr(b)}


def calendar_years(nav: pd.DataFrame) -> dict:
    """Return per calendar year, from the last mark of the previous year (or the start)."""
    out = {}
    d = nav.copy()
    d["year"] = d["date"].dt.year
    prev = d["nav"].iloc[0]
    for y, g in d.groupby("year"):
        end = g["nav"].iloc[-1]
        out[int(y)] = float(end / prev - 1)
        prev = end
    return out


def turnover(trades: pd.DataFrame, nav: pd.DataFrame, exclude_initial: bool = True) -> float:
    """Annual one-way turnover: (buys + sells) / 2 / average NAV / years.

    The initial build is excluded by default because it is not turnover in any
    useful sense; it is reported both ways in metrics.json. The initial build
    is every fill from the first decision date's orders, which may fill up to
    5 market sessions after it (Clarifications 10 and 18), so trades on or
    before the 5th session after the start are excluded.
    """
    if trades is None or trades.empty:
        return 0.0
    t = trades[trades["side"].isin(["buy", "sell"])]
    if exclude_initial:
        sess = pd.DatetimeIndex(sorted(pd.to_datetime(nav["date"]).unique()))
        cutoff = sess[min(5, len(sess) - 1)]
        t = t[pd.to_datetime(t["date"]) > cutoff]
    years = (nav["date"].iloc[-1] - nav["date"].iloc[0]).days / 365.25
    return float(t["value"].sum() / 2 / nav["nav"].mean() / years)


def holding_stats(episodes: pd.DataFrame) -> dict:
    if episodes is None or episodes.empty:
        return {"episodes": 0, "avg_holding_days": np.nan, "doubled": 0}
    return {"episodes": int(len(episodes)),
            "avg_holding_days": float(episodes["days_held"].mean()),
            "doubled": int(episodes["doubled"].sum()),
            "open_at_end": int((episodes["exit_reason"] == "open_at_end").sum()),
            "no_trade_exits": int((episodes["exit_reason"] == "no_trade_exit").sum())}


def block_bootstrap_ci(ra: np.ndarray, rb: np.ndarray, block: int = 21, n_boot: int = 5000,
                       level: float = 0.90, seed: int = 20260921) -> dict:
    """Circular block bootstrap of the annualised return difference.

    Pairs of daily returns (strategy, benchmark) are resampled together in
    blocks of `block` sessions, preserving autocorrelation and the cross
    correlation between the two. The statistic is the difference in
    annualised geometric return.
    """
    ra, rb = np.asarray(ra, float), np.asarray(rb, float)
    n = len(ra)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    stats = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        ga = np.exp(np.log1p(ra[idx]).sum() * PERIODS / n) - 1
        gb = np.exp(np.log1p(rb[idx]).sum() * PERIODS / n) - 1
        stats[i] = ga - gb
    lo, hi = np.quantile(stats, [(1 - level) / 2, 1 - (1 - level) / 2])
    point = (np.exp(np.log1p(ra).sum() * PERIODS / n) - 1) - (np.exp(np.log1p(rb).sum() * PERIODS / n) - 1)
    return {"point": float(point), "lo": float(lo), "hi": float(hi), "level": level,
            "block": block, "n_boot": n_boot, "p_le_0": float((stats <= 0).mean())}


def summary(nav: pd.DataFrame) -> dict:
    r = returns(nav)
    return {"cagr": cagr(nav), "volatility": vol(r), "sharpe_rf0": sharpe(r),
            "max_drawdown": max_drawdown(nav), "calendar_years": calendar_years(nav),
            "start": str(nav["date"].iloc[0].date()), "end": str(nav["date"].iloc[-1].date()),
            "n_returns": int(len(r)), "final_multiple": float(nav["nav"].iloc[-1] / nav["nav"].iloc[0])}


def full_report(strategy, benchmarks: dict[str, pd.DataFrame], trades=None, episodes=None,
                bootstrap_vs: str | None = "universe_ew") -> dict:
    out = summary(strategy)
    out["turnover_annual"] = turnover(trades, strategy, exclude_initial=True)
    out["turnover_annual_incl_initial"] = turnover(trades, strategy, exclude_initial=False)
    out.update(holding_stats(episodes))
    out["vs"] = {}
    for name, b in benchmarks.items():
        if b is None or b.empty:
            continue
        out["vs"][name] = {**summary(b), **active(strategy, b)}
    if bootstrap_vs and bootstrap_vs in benchmarks:
        ra, rb = align(strategy, benchmarks[bootstrap_vs])
        out["bootstrap_vs_" + bootstrap_vs] = block_bootstrap_ci(ra, rb)
    return out

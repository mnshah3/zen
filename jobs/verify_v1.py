"""Is a backtest result skill, or is it luck wearing a suit?

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
  stock's own turnover.

Corrected on 2026-09-23 after an independent audit: the factor regression had
subtracted the risk-free rate twice, the final test was measured from the wrong
mark, calendar years skipped their first session, the random portfolios were
compared at a much higher turnover than the strategy, and the deflated Sharpe
was reported as one number when it depends heavily on modelling choices.

Generic over a run folder since 2026-09-26 (v2-spec A2: v2 is judged against v1
with the same measurement jobs). Every figure except the monkey test is read
from the run's own files (nav.csv, trades.csv, ranks.parquet, metrics.json,
in_sample_end_open.csv); nothing is re-simulated for them. The monkey test
replays the run through v1's engine with the configuration in metrics.json,
and first checks that doing so reproduces the run's own NAV to 1e-9: if it
does not, the random portfolios would not be the same machinery, and the job
stops rather than compare unlike things.

    python -m jobs.verify_v1 --run data/backtest/v1_final --label v1   # 500 monkeys
    python -m jobs.verify_v1 --run ... --monkeys 0                       # files only

Writes <run>/verify.json and <run>/verify_monkey_{persistent,fresh}.csv (or --out DIR).
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs import libcheck_v1 as lc  # noqa: E402
from zen.universe import pit  # noqa: E402
from zen.validation import trials  # noqa: E402

log = logging.getLogger(__name__)

IIMA = lc.IIMA
GRID = ROOT / "data" / "backtest" / "v1" / "grid.csv"
REPRODUCE_TOL = 1e-9


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
    return (nav.iloc[-1] / nav.iloc[0]) ** (1 / lc.years_between(nav.index[0], nav.index[-1])) - 1


# ---------------------------------------------------------------- the run's files
def load_run(run: Path) -> dict:
    """Everything the file-based figures need from a run folder."""
    run = Path(run)
    clock = lc.load_clock(run)
    metrics = json.loads((run / "metrics.json").read_text()) if (run / "metrics.json").exists() else {}
    trades = pd.read_csv(run / "trades.csv")
    trades["date"] = pd.to_datetime(trades["date"])
    ranks = pd.read_parquet(run / "ranks.parquet")
    ranks["D"] = pd.to_datetime(ranks["D"])
    return {"run": run, "clock": clock, "metrics": metrics, "trades": trades, "ranks": ranks,
            "strategy": clock["strategy"].dropna(), "universe_ew": clock["universe_ew"].dropna()}


# ---------------------------------------------------------------- monkey test
def load_engine(con, r: dict):
    """The run's own engine inputs: decision dates and config from metrics.json,
    ranks.parquet, and the panel built as jobs/backtest_v1.py builds it."""
    from zen.portfolio import engine

    m = r["metrics"]
    if "config" not in m or "decision_dates" not in m:
        raise ValueError("metrics.json has no config or decision dates: cannot replay this run")
    try:
        cfg = engine.Config(**m["config"])
    except TypeError as e:
        raise ValueError(f"metrics.json's config is not a v1 engine config: {e}") from e
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    decisions = [pd.Timestamp(d) for d in m["decision_dates"]]
    end = r["clock"].index[-1].normalize()
    run_final_test = bool(end > is_end)
    ranks = r["ranks"][r["ranks"]["D"].isin(decisions)]
    static = pit.StaticLabels.load(con)
    panel = engine.build_panel(con, ranks["symbol"].unique(), decisions[0], end, is_end,
                               run_final_test, ids=static.ids)
    return engine, cfg, is_end, decisions, ranks, panel, run_final_test


def reproduce(engine, panel, ranks, decisions, cfg, is_end, run_final_test,
              nav_strategy: pd.Series):
    """Re-run the strategy and require the run's own NAV, mark for mark."""
    res = engine.run_strategy(panel, ranks, decisions, cfg, is_end, run_final_test)
    a = res.nav["nav"].to_numpy(float)
    b = nav_strategy.to_numpy(float)
    if len(a) != len(b):
        raise RuntimeError(f"replay has {len(a)} marks, the run {len(b)}: not the same machinery")
    worst = float(np.max(np.abs(a / b - 1)))
    if worst > REPRODUCE_TOL:
        raise RuntimeError(f"replay differs from nav.csv by up to {worst:.3g}: "
                           "not the same machinery; the monkey test would be meaningless")
    return res, worst


def monkeys(engine, panel, ranks, decisions, cfg, is_end, n_draws: int, mode: str = "persistent",
            seed: int = 20260922, run_final_test: bool = True):
    """Random portfolios through the same machinery.

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
                              choose, cfg, is_end, run_final_test=run_final_test, log_trades=True)
        nav = res.nav.set_index("date")["nav"]
        out.append(cagr(nav))
        turn.append(turnover(res.trades, nav))
        if (i + 1) % 100 == 0:
            log.info("  %d/%d monkeys (%s)", i + 1, n_draws, mode)
    return np.array(out), np.array(turn)


# ---------------------------------------------------------------- turnover
def turnover(trades: pd.DataFrame, nav: pd.Series) -> float:
    """Annual one-way turnover: half of everything bought and sold, per year,
    over the average value of the book. Includes the initial build."""
    if trades.empty or "value" not in trades:
        return 0.0
    yrs = lc.years_between(nav.index[0], nav.index[-1])
    return float(trades["value"].abs().sum() / 2 / nav.mean() / yrs)


# ---------------------------------------------------------------- attribution
def attribution(strategy_nav: pd.Series, ew_nav: pd.Series) -> dict:
    """Regress monthly excess returns on IIMA's published Indian factors.

    If the strategy is only size and momentum in disguise, the factor loadings
    absorb the return and alpha collapses. Newey-West errors with 3 lags,
    because monthly portfolio returns are not independent. Written by hand;
    jobs/libcheck_v1.py recomputes it with statsmodels.
    """
    if not IIMA.exists():
        return {"error": "IIMA factor file missing; see data/external/SOURCES.md"}
    f = lc.load_iima()

    out = {}
    for name, nav in (("strategy", strategy_nav), ("universe_ew", ew_nav)):
        m = lc.monthly_returns(nav)
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


# ---------------------------------------------------------------- capacity
def capacity(trades: pd.DataFrame, ranks: pd.DataFrame) -> dict:
    """Could these trades have been done at the printed price? (fills ON a decision date)

    Only trades dated on a decision date on which the stock was in the universe
    are matched to the ranks' 60-session median turnover; a fill up to five
    sessions later, a forced exit and a sale of a stock that left the universe
    are not counted. Kept as the figure earlier reports quoted; see
    capacity_all_trades for every trade.
    """
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
    return {"trades": int(len(j)), "matched": int(len(ok)),
            "median_pct_of_daily_turnover": round(float(ok.median() * 100), 3),
            "p95_pct": round(float(ok.quantile(0.95) * 100), 3),
            "max_pct": round(float(ok.max() * 100), 3),
            "trades_over_5pct": int((ok > 0.05).sum()),
            "note": "at the backtest's Rs 5 lakh. Ten times the capital multiplies these by ten."}


def median_turnover_at(con, pairs: pd.DataFrame, ids=None,
                       window: int = pit.TURNOVER_WINDOW) -> np.ndarray:
    """Universe rule 2's median daily turnover for each (symbol, date) in `pairs`.

    Over the `window` sessions strictly before the date, untraded sessions as 0,
    from the same price rows as zen/universe/pit.py (close > 0, mapped to stock
    ids, a rename overlap keeping the row with the larger turnover). Only what
    was known before the session's open is used.
    """
    days = pd.to_datetime(pairs["date"]).dt.normalize().to_numpy()
    cal = pit.sessions(con)
    first = int(cal.searchsorted(days.min()))
    if first < window:
        raise ValueError(f"fewer than {window} sessions before {pd.Timestamp(days.min()).date()}")
    lo, hi = cal[first - window], pd.Timestamp(days.max())
    px = con.execute("SELECT symbol, date, isin_code, turnover FROM prices "
                     "WHERE date >= ? AND date < ? AND close > 0", [lo.date(), hi.date()]).df()
    px["date"] = pd.to_datetime(px["date"])
    px["ticker"] = px["symbol"]
    if ids is not None and not ids.empty:
        px["symbol"] = ids.for_prices(px)
    px = px[px["symbol"].isin(set(pairs["symbol"]))]
    px = (px.sort_values(["turnover", "ticker"], ascending=[False, True])
            .drop_duplicates(["symbol", "date"]))
    sess = cal[(cal >= lo) & (cal < hi)]
    wide = (px.pivot(index="date", columns="symbol", values="turnover")
              .reindex(sess).fillna(0.0))
    out = np.empty(len(days))
    for k, (sym, day) in enumerate(zip(pairs["symbol"], days)):
        i = int(sess.searchsorted(day))
        if i < window:
            raise ValueError(f"fewer than {window} sessions before {pd.Timestamp(day).date()}")
        out[k] = (float(np.median(wide[sym].to_numpy()[i - window:i]))
                  if sym in wide.columns else 0.0)
    return out


def check_against_ranks(trades: pd.DataFrame, med: np.ndarray, ranks: pd.DataFrame) -> dict:
    """The recomputed median must equal the ranks' own on every trade dated on a
    decision date on which the stock was in the universe; otherwise it is not rule 2."""
    t = trades[["symbol", "date"]].copy()
    t["med"] = med
    j = t.merge(ranks[["symbol", "D", "median_turnover_60"]],
                left_on=["symbol", "date"], right_on=["symbol", "D"], how="inner")
    if j.empty:
        return {"matched": 0, "worst_rel": None}
    rel = np.abs(j["med"] / j["median_turnover_60"] - 1)
    worst = float(rel.max())
    if worst > 1e-9:
        bad = j.loc[rel.idxmax()]
        raise RuntimeError(f"recomputed median turnover differs from ranks.parquet by {worst:.3g} "
                           f"({bad['symbol']} {bad['date'].date()})")
    return {"matched": int(len(j)), "worst_rel": worst}


def capacity_all_trades(trades: pd.DataFrame, med: np.ndarray) -> dict:
    """v2-spec A2 rule 5: the 95th percentile, over every buy and sell, of trade
    value as a share of the stock's 60-session median turnover before the trade."""
    t = trades.copy()
    t["median_turnover_60"] = med
    zero = int((t["median_turnover_60"] <= 0).sum())
    t["share"] = t["value"].abs() / t["median_turnover_60"].where(t["median_turnover_60"] > 0)
    # A trade in a stock with no turnover at all is the least liquid possible:
    # it counts as an infinite share, never as a missing one.
    s = t["share"].fillna(np.inf).to_numpy(float)

    def p95_of(x) -> float:
        with np.errstate(invalid="ignore"):  # interpolating between two infinities gives nan
            q = float(np.quantile(x, 0.95))
        return float("inf") if np.isnan(q) else q

    p95 = p95_of(s)
    by_side = {side: p95_of(g["share"].fillna(np.inf).to_numpy(float))
               for side, g in t.groupby("side")}
    return {"trades": int(len(t)), "zero_turnover_trades": zero,
            "p95": p95, "p95_pct": round(p95 * 100, 3),
            "median_pct": round(float(np.median(s)) * 100, 3),
            "max_pct": round(float(np.max(s)) * 100, 3),
            "trades_over_5pct": int((s > 0.05).sum()),
            "p95_by_side": by_side,
            "trades_by_reason": t["reason"].value_counts().to_dict() if "reason" in t else {},
            "definition": "every buy and sell in trades.csv (cancellations excluded): |value| / "
                          "median daily turnover of the stock over the 60 sessions before the "
                          "trade date, untraded sessions as 0 (universe rule 2's measure, "
                          "recomputed at each trade date); 95th percentile, linear "
                          "interpolation; at the backtest's Rs 5 lakh starting capital"}


# ---------------------------------------------------------------- sub-periods
def subperiods(run: Path, s_nav: pd.Series, e_nav: pd.Series) -> dict:
    """Open of the first decision date to open of the in-sample end, then open of
    the in-sample end to open of the end date (v1 Clarification 16, disclosure 37)."""
    sv = lc.split_values(run)
    if sv is None:
        return {"note": "no in_sample_end_open.csv in this run"}
    split, vals = sv
    if s_nav.index[-1].normalize() <= split:
        return {"note": f"the run ends before the open of {split.date()}"}

    def seg(v0, v1, t0, t1):
        return (v1 / v0) ** (1 / lc.years_between(t0, t1)) - 1

    sub = {}
    for label, t0, v0s, v0e, t1, v1s, v1e in [
            ("in_sample", s_nav.index[0], s_nav.iloc[0], e_nav.iloc[0],
             split, vals["strategy"], vals["universe_ew"]),
            ("final_test", split, vals["strategy"], vals["universe_ew"],
             s_nav.index[-1], s_nav.iloc[-1], e_nav.iloc[-1])]:
        a_, b_ = seg(v0s, v1s, t0, t1), seg(v0e, v1e, t0, t1)
        sub[label] = {"strategy_cagr_pct": round(a_ * 100, 2),
                      "universe_ew_cagr_pct": round(b_ * 100, 2),
                      "diff_pct": round((a_ - b_) * 100, 2),
                      "from_open": str(pd.Timestamp(t0).date()), "to_open": str(pd.Timestamp(t1).date())}
    return sub


def by_year(s_nav: pd.Series, e_nav: pd.Series) -> dict:
    """Calendar years from the previous year-end value, not from the first mark
    of the year, which skipped the first session of every year."""
    yearly = {}
    s_y = s_nav.groupby(s_nav.index.year).last()
    e_y = e_nav.groupby(e_nav.index.year).last()
    prev_s, prev_e = s_nav.iloc[0], e_nav.iloc[0]
    for y in s_y.index:
        yearly[int(y)] = {"strategy_pct": round((s_y[y] / prev_s - 1) * 100, 1),
                          "universe_ew_pct": round((e_y[y] / prev_e - 1) * 100, 1)}
        prev_s, prev_e = s_y[y], e_y[y]
    return yearly


def deflated(s_nav: pd.Series, grid_path: Path = GRID) -> dict:
    """Deflated Sharpe three ways. The answer depends on which variance of trial
    Sharpes is used and how many trials are counted, and none of the choices is
    obviously right here, so the spread is what gets reported."""
    daily = s_nav.pct_change().dropna()
    n_trials = trials.lifetime()
    sr = float(daily.mean() / daily.std())
    g1, g2 = float(daily.skew()), float(daily.kurt()) + 3.0
    grid = pd.read_csv(grid_path)
    cross_var = float((grid["sharpe_rf0"] / np.sqrt(252)).var())
    return {"sharpe_annual": sr * np.sqrt(252), "n_obs": int(len(daily)), "n_trials": n_trials,
            "prob_null_sampling_variance": trials.deflated_sharpe(sr, n_trials, len(daily), g1, g2),
            "prob_cross_trial_variance_36_grid": trials.deflated_sharpe(
                sr, n_trials, len(daily), g1, g2, var_trial_sharpe=cross_var),
            "prob_grid_trials_only": trials.deflated_sharpe(sr, len(grid), len(daily), g1, g2),
            "grid_file": str(grid_path),
            "note": "probability of genuine skill after the search; the spread between these "
                    "is the honest answer, not any single one"}


# ---------------------------------------------------------------- main
def run_verify(run: Path, label: str, n_monkeys: int, out: Path, con=None) -> dict:
    r = load_run(run)
    s_nav, e_nav = r["strategy"], r["universe_ew"]
    s_cagr, e_cagr = cagr(s_nav), cagr(e_nav)
    con = con or pit.connect()
    static = pit.StaticLabels.load(con)
    trades = r["trades"][r["trades"]["side"].isin(["buy", "sell"])].reset_index(drop=True)

    monkey = {"strategy_cagr_pct": round(s_cagr * 100, 2),
              "strategy_turnover": round(turnover(r["trades"], s_nav), 2)}
    if n_monkeys > 0:
        engine, cfg, is_end, decisions, ranks, panel, rft = load_engine(con, r)
        _, worst = reproduce(engine, panel, ranks, decisions, cfg, is_end, rft, s_nav)
        monkey["replay_worst_rel"] = worst
        for mode in ("persistent", "fresh"):
            log.info("monkey test (%s): %d random portfolios", mode, n_monkeys)
            mk, mt = monkeys(engine, panel, ranks, decisions, cfg, is_end, n_monkeys, mode=mode,
                             run_final_test=rft)
            monkey[mode] = {"draws": int(len(mk)),
                            "random_median_pct": round(float(np.median(mk)) * 100, 2),
                            "random_p5_pct": round(float(np.percentile(mk, 5)) * 100, 2),
                            "random_p95_pct": round(float(np.percentile(mk, 95)) * 100, 2),
                            "random_best_pct": round(float(mk.max()) * 100, 2),
                            "random_median_turnover": round(float(np.median(mt)), 2),
                            "strategy_percentile": round(float((mk < s_cagr).mean() * 100), 1),
                            "beaten_by_n_random": int((mk >= s_cagr).sum())}
            pd.DataFrame({"cagr": mk, "turnover": mt}).to_csv(
                out / f"verify_monkey_{mode}.csv", index=False)
    else:
        monkey["note"] = "not run (--monkeys 0)"

    med = median_turnover_at(con, trades, static.ids)
    report = {"run": str(run), "label": label,
              "period": {"start": str(s_nav.index[0].date()), "end": str(s_nav.index[-1].date())},
              "headline": {"strategy_cagr_pct": round(s_cagr * 100, 2),
                           "universe_ew_cagr_pct": round(e_cagr * 100, 2)},
              "monkey_test": monkey, "attribution": attribution(s_nav, e_nav),
              "deflated_sharpe": deflated(s_nav),
              "subperiods": subperiods(run, s_nav, e_nav), "by_year": by_year(s_nav, e_nav),
              "turnover_annual_incl_initial": turnover(r["trades"], s_nav),
              "capacity": capacity(r["trades"], r["ranks"]),
              "capacity_all_trades": {**capacity_all_trades(trades, med),
                                      "check_against_ranks": check_against_ranks(
                                          trades, med, r["ranks"])}}
    return report


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="data/backtest/v1_final", help="backtest output folder")
    ap.add_argument("--label", default=None, help="name for this run (default: folder name)")
    ap.add_argument("--monkeys", type=int, default=500, help="draws per mode; 0 skips the test")
    ap.add_argument("--out", default=None, help="output folder (default: the run folder)")
    args = ap.parse_args(argv)
    run = Path(args.run)
    out = Path(args.out) if args.out else run
    out.mkdir(parents=True, exist_ok=True)
    rep = run_verify(run, args.label or run.name, args.monkeys, out)
    (out / "verify.json").write_text(json.dumps(rep, indent=2, default=float))

    monkey, attr, dsr = rep["monkey_test"], rep["attribution"], rep["deflated_sharpe"]
    print(f"\n=== {rep['label']}: MONKEY TEST ===")
    print(f"  strategy {monkey['strategy_cagr_pct']}%/yr, turnover {monkey['strategy_turnover']}x/yr")
    for mode in ("persistent", "fresh"):
        if mode not in monkey:
            continue
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
    for k, v in rep["subperiods"].items():
        if isinstance(v, dict):
            print(f"  {k}: {v['strategy_cagr_pct']}% vs EW {v['universe_ew_cagr_pct']}% "
                  f"({v['diff_pct']:+}pp)")
    print("\n=== CAPACITY ===")
    print("  decision-date fills:", rep["capacity"])
    c = rep["capacity_all_trades"]
    print(f"  every trade: p95 {c['p95_pct']}% of 60-session median turnover over "
          f"{c['trades']} trades")
    print(f"\nwritten to {out}/verify.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

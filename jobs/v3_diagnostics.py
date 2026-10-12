"""Two diagnostics of a v3 run that need prices (v3 spec, sections 14.4 and 14.5).

    python -m jobs.v3_diagnostics --run data/backtest/v3 --v1-replay data/backtest/v1_replay_v3

Writes <run>/diagnostics.json:

  score_signal            section 14.4. The rank IC of the score against the next review
                          period's return across all eligible stocks, at each review (the
                          27th is reported and left out of the test), for T and for R1, R2
                          and R3; and the top-minus-bottom quintile spread through
                          engine.run_quantile (v1's execution rules, no costs).
  earnings_and_rerating   section 14.5. Each review interval's price growth of the stocks
                          held at its end, split into the growth of their look-through
                          earnings and the change in the book's P/E; chained and annualised;
                          for v3 and for v1 (the window view of its replay), with the
                          Nifty 500's own split from NSE's published P/E for context.

Everything is read from the run (its ranks, figures, trades and nav) and from the inputs
the run itself read (the frozen directory in its metrics.json). The one thing computed
afresh is the snapshot at the end date, 18 September 2026, built like any review's.
It changes nothing in the run and records no trial.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from jobs import backtest_v3
from zen.portfolio import engine, engine_v2, engine_v3, metrics
from zen.universe import pit

log = logging.getLogger("v3_diagnostics")
IC_TEST_REVIEWS = 26


# ---------------------------------------------------------------- 14.4 rank IC
def value_at(panel: engine.Panel, t: int) -> np.ndarray:
    """V(d) for every stock: the adjusted open if it trades at d (the BZ open on a
    trade-for-trade session), else its last adjusted close before d."""
    prev = panel.last[t - 1] if t > 0 else np.full(len(panel.symbols), np.nan)
    o = panel.open_[t]
    return np.where(panel.traded[t] & np.isfinite(o), o, prev)


def forward_returns(panel: engine.Panel, t0: int, t1: int) -> np.ndarray:
    """(V(D') + dividends) / V(D) - 1 for every stock: dividends are the cash per adjusted
    unit with ex-dates in (D, D']."""
    div = np.nansum(panel.div_unit[t0 + 1:t1 + 1], axis=0)
    v0, v1 = value_at(panel, t0), value_at(panel, t1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (v1 + div) / v0 - 1


def spearman(x, y) -> float:
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3:
        return float("nan")
    return float(stats.spearmanr(x[ok], y[ok]).statistic)


def ic_summary(ics: list[float], n_test: int = IC_TEST_REVIEWS) -> dict:
    """Mean IC, t = mean / (sd with ddof 1 / sqrt(n)) and the share positive, over the
    first `n_test` reviews (a Fama-MacBeth-style test)."""
    x = np.asarray(ics[:n_test], dtype=float)
    if len(x) != n_test or not np.isfinite(x).all():
        raise ValueError(f"need {n_test} finite ICs, got {len(x)} with "
                         f"{int((~np.isfinite(x)).sum())} missing")
    sd = float(np.std(x, ddof=1))
    return {"reviews": int(n_test), "mean": float(x.mean()),
            "t": float(x.mean() / (sd / np.sqrt(n_test))) if sd > 0 else float("nan"),
            "share_positive": float((x > 0).mean())}


def score_signal(panel: engine.Panel, ranks: pd.DataFrame, decisions: list, end: pd.Timestamp,
                 is_end, run_final_test: bool) -> dict:
    marks = list(decisions) + [pd.Timestamp(end)]
    t = [panel.dates.get_loc(d) for d in marks]
    cols = ("T", "R1", "R2", "R3")
    per, ics = [], {c: [] for c in cols}
    for k, D in enumerate(decisions):
        g = ranks[ranks["D"] == D]
        fwd = forward_returns(panel, t[k], t[k + 1])[[panel.col(s) for s in g["symbol"]]]
        row = {"D": str(D.date()), "to": str(marks[k + 1].date()), "eligible": int(len(g)),
               "without_a_forward_return": int((~np.isfinite(fwd)).sum())}
        for c in cols:
            ic = spearman(g[c].to_numpy(float), fwd)
            ics[c].append(ic)
            row[f"ic_{c}"] = ic
        per.append(row)
    # the full run tests the first 26 of its 27 reviews; a development run (the agreement
    # period, whose last review period is a whole one) tests every review it has
    n_test = IC_TEST_REVIEWS if len(decisions) == 27 else len(decisions)
    out = {"ic": {c: ic_summary(ics[c], n_test) for c in cols}, "by_review": per,
           "note": "the 27th review's period (17 August 2026 to the end) is shorter than a "
                   "review period: its IC is reported in by_review and left out of the test"}
    # quintiles: the section-6 order, cut into five groups whose sizes differ by at most one
    r = ranks[["D", "symbol", "rank"]].copy()
    r["neg_rank"] = -r["rank"].astype(float)
    top = engine.run_quantile(panel, r, decisions, "neg_rank", 1, is_end, 5, run_final_test)
    bot = engine.run_quantile(panel, r, decisions, "neg_rank", 5, is_end, 5, run_final_test)
    act = metrics.active(top.nav, bot.nav)
    out["quintiles"] = {"top_cagr": metrics.cagr(top.nav), "bottom_cagr": metrics.cagr(bot.nav),
                        "cagr_spread": metrics.cagr(top.nav) - metrics.cagr(bot.nav),
                        "information_ratio": act["information_ratio"],
                        "tracking_error": act["tracking_error"],
                        "method": "engine.run_quantile on minus the section-6 rank: v1's "
                                  "execution rules, no costs, dividends as cash, the 20-session "
                                  "exit; metrics.active(top, bottom)"}
    return out


# ---------------------------------------------------------------- 14.5 earnings and re-rating
def split_interval(v0: float, v1: float, e0: float, e1: float) -> dict:
    """One interval: V_end / V_start = (E_end / E_start) x [(V_end / E_end) / (V_start / E_start)].
    Undefined unless both earnings sums are above zero."""
    if not (e0 > 0 and e1 > 0 and v0 > 0 and v1 > 0):
        return {"defined": False}
    return {"defined": True, "price": v1 / v0, "earnings": e1 / e0,
            "rerating": (v1 / e1) / (v0 / e0)}


def chain(intervals: list[dict], days: int) -> dict:
    """G_E and G_PE over the defined intervals, each annualised over `days` calendar days,
    and the share ln G_E / (ln G_E + ln G_PE)."""
    d = [x for x in intervals if x.get("defined")]
    ge = float(np.prod([x["earnings"] for x in d])) if d else float("nan")
    gp = float(np.prod([x["rerating"] for x in d])) if d else float("nan")
    tot = np.log(ge) + np.log(gp) if d else float("nan")
    return {"intervals": len(intervals), "undefined_intervals": len(intervals) - len(d),
            "earnings_factor": ge, "rerating_factor": gp,
            "earnings_annual": ge ** (365.25 / days) - 1 if d else None,
            "rerating_annual": gp ** (365.25 / days) - 1 if d else None,
            "share_from_earnings": float(np.log(ge) / tot) if d and tot != 0 else None}


def units_at(trades: pd.DataFrame, session: pd.Timestamp) -> pd.Series:
    """Adjusted units of every stock held at the close of `session`, from trades.csv."""
    t = trades[trades["date"] <= session]
    u = (t["units"] * np.where(t["side"] == "buy", 1.0, -1.0)).groupby(t["symbol"]).sum()
    return u[u > 1e-9]


def earnings_split(panel: engine.Panel, trades: pd.DataFrame, figs: dict, marks: list,
                   nav_start: float, nav_end: float) -> dict:
    """`marks` are D_1..D_27 and D_28 (the end date); `figs[D]` gives each stock's TTM_P and
    market cap as known at D. Values are at the close of the session before each mark."""
    t_prev = [panel.dates.get_loc(d) - 1 for d in marks]
    rows = []
    for k in range(len(marks) - 1):
        u = units_at(trades, panel.dates[t_prev[k + 1]])
        f0, f1 = figs[marks[k]], figs[marks[k + 1]]
        v0 = v1 = e0 = e1 = left = total = 0.0
        for s, units in u.items():
            c = panel.col(s)
            a, b = units * panel.last[t_prev[k], c], units * panel.last[t_prev[k + 1], c]
            total += b if np.isfinite(b) else 0.0
            ok = s in f0.index and s in f1.index
            if ok:
                p0, m0, p1, m1 = (float(f0.at[s, "ttm_p"]), float(f0.at[s, "mcap"]),
                                  float(f1.at[s, "ttm_p"]), float(f1.at[s, "mcap"]))
                ok = all(np.isfinite(x) for x in (p0, m0, p1, m1, a, b)) and m0 > 0 and m1 > 0
            if not ok:
                left += b if np.isfinite(b) else 0.0
                continue
            v0, v1 = v0 + a, v1 + b
            e0, e1 = e0 + a * p0 / m0, e1 + b * p1 / m1
        r = split_interval(v0, v1, e0, e1)
        r.update({"from": str(marks[k].date()), "to": str(marks[k + 1].date()), "holdings": int(len(u)),
                  "share_of_value_left_out": left / total if total > 0 else None})
        rows.append(r)
    days = (panel.dates[t_prev[-1]] - panel.dates[t_prev[0]]).days
    out = chain(rows, days)
    out["remainder"] = (nav_end / nav_start / (out["earnings_factor"] * out["rerating_factor"])
                        if out["earnings_factor"] == out["earnings_factor"] else None)
    out["clock"] = {"from_close": str(panel.dates[t_prev[0]].date()),
                    "to_close": str(panel.dates[t_prev[-1]].date()), "calendar_days": int(days)}
    out["by_interval"] = rows
    return out


def index_split(con, name: str, d0: pd.Timestamp, d1: pd.Timestamp) -> dict:
    """The Nifty 500's own split from NSE's published P/E (another earnings definition, so
    not comparable in level)."""
    q = con.execute("SELECT date, close, pe FROM indices WHERE index_name = ? AND date IN (?, ?) "
                    "ORDER BY date", [name, d0.date(), d1.date()]).df()
    if len(q) != 2 or q["pe"].isna().any():
        return {"available": False}
    days = (d1 - d0).days
    price, rer = float(q["close"].iloc[1] / q["close"].iloc[0]), float(q["pe"].iloc[1] / q["pe"].iloc[0])
    return {"available": True, "index": name, "price_factor": price, "rerating_factor": rer,
            "earnings_factor": price / rer,
            "earnings_annual": (price / rer) ** (365.25 / days) - 1,
            "rerating_annual": rer ** (365.25 / days) - 1,
            "note": "NSE's published P/E: a different earnings definition, for context only"}


# ---------------------------------------------------------------- main
def run(run_dir: Path, v1_replay: Path | None) -> dict:
    run_dir = Path(run_dir)
    m = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    if m.get("engine") != "v3":
        raise ValueError(f"{run_dir}: not a v3 run")
    frozen = (m.get("fingerprint") or {}).get("frozen")
    paths = backtest_v3.input_paths(frozen)
    if frozen:
        backtest_v3.verify_frozen(frozen)
    con = pit.connect(paths["db"])
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    end, rft = pd.Timestamp(m["end_open"]), bool(m["run_final_test"])
    engine.guard(end, is_end, rft)
    static = backtest_v3.static_labels(con, paths["nse_list"])
    keys = engine_v3.sector_keys(engine_v2.nse_sectors(pd.read_parquet(paths["labels"]), static.ids))
    tickers = engine_v2.Tickers(static.ids)
    decisions = [pd.Timestamp(d) for d in m["decision_dates"]]

    ranks = pd.read_parquet(run_dir / "ranks.parquet")
    ranks["D"] = pd.to_datetime(ranks["D"])
    ranks = ranks[ranks["D"].isin(decisions)]
    figs_all = pd.read_parquet(run_dir / "figures.parquet")
    figs_all["D"] = pd.to_datetime(figs_all["D"])
    figs = {D: figs_all[figs_all["D"] == D].set_index("symbol") for D in decisions}
    log.info("the snapshot at the end date %s", end.date())
    figs[end] = engine_v3.compute_review(con, end, static, keys, tickers).figs

    def trades_of(folder: Path) -> pd.DataFrame:
        t = pd.read_csv(folder / "trades.csv", parse_dates=["date"])
        return t[t["side"].isin(["buy", "sell"])]

    t3 = trades_of(run_dir)
    t1 = trades_of(Path(v1_replay)) if v1_replay else None
    syms = set(ranks["symbol"]) | set(t3["symbol"]) | (set(t1["symbol"]) if t1 is not None else set())
    panel = engine.build_panel(con, sorted(syms), decisions[0], end, is_end, rft, ids=static.ids)
    out = {"run": str(run_dir), "frozen": frozen, "end_open": str(end.date()),
           "score_signal": score_signal(panel, ranks, decisions, end, is_end, rft)}

    marks = decisions + [end]
    nav = pd.read_csv(run_dir / "nav.csv", parse_dates=["date"])
    last_close = nav[nav["mark"] == "close"]["strategy"].iloc[-1]
    split = {"v3": earnings_split(panel, t3, figs, marks, float(nav["strategy"].iloc[0]),
                                  float(last_close))}
    if t1 is not None:
        n1 = pd.read_csv(Path(v1_replay) / "nav.csv", parse_dates=["date"])
        ro = pd.read_csv(Path(v1_replay) / "rebalance_open.csv", parse_dates=["date"])
        start = float(ro[ro["date"] == decisions[0]]["strategy_open"].iloc[0])
        c1 = n1[(n1["mark"] == "close") & (n1["date"] < end)]["strategy"]
        split["v1"] = earnings_split(panel, t1, figs, marks, start, float(c1.iloc[-1]))
    t_prev = [panel.dates.get_loc(d) - 1 for d in (marks[0], marks[-1])]
    split["nifty500"] = index_split(con, "Nifty 500", panel.dates[t_prev[0]], panel.dates[t_prev[1]])
    out["earnings_and_rerating"] = split
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=str(backtest_v3.BACKTEST / "v3"))
    ap.add_argument("--v1-replay", default=str(backtest_v3.BACKTEST / "v1_replay_v3"),
                    help="v1's replay on the frozen inputs (its trades from 2019 give the book "
                         "at every review of the window)")
    ap.add_argument("--out", default=None, help="output file (default <run>/diagnostics.json)")
    a = ap.parse_args(argv)
    rep = run(Path(a.run), Path(a.v1_replay) if a.v1_replay else None)
    out = Path(a.out) if a.out else Path(a.run) / "diagnostics.json"
    out.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    ic, q = rep["score_signal"]["ic"]["T"], rep["score_signal"]["quintiles"]
    print(f"IC mean {ic['mean']:.4f}, t {ic['t']:.2f}; quintile spread {q['cagr_spread'] * 100:+.2f}% a year")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

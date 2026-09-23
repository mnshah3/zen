"""The full picture of what v1 actually did.

Nothing here fits anything. Every number describes the run that already
happened, which is why it carries no overfitting risk and can be read freely
before deciding what v2 should change.

A CAGR and a Sharpe hide the things that decide whether a person can actually
hold a strategy:

  HOW LONG THE PAIN LASTS. A 30% fall that recovers in three months and one
  that takes two years are different experiences. Drawdowns are reported with
  their start, trough, recovery and length in months.

  WHAT HAPPENS WHEN THE MARKET FALLS. Up and down capture against the Nifty 500
  total return say whether the strategy is defensive or simply more of
  everything.

  WHETHER THE WINS ARE BROAD OR NARROW. Per-position outcomes: how many made
  money, the median, and how much of the total return came from the best five.
  A strategy carried by two holdings is a different bet from one where most
  positions work.

  WHERE THE MONEY WAS EXPOSED. Sector weights over time, and how concentrated
  the book was.

    python -m jobs.stats_v1
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

RUN = Path("data/backtest/v1_holdout")
OUT = Path("data/backtest/stats")
TRI = Path("data/external/nifty_tri.parquet")


def drawdowns(nav: pd.Series, top: int = 5) -> pd.DataFrame:
    peak = nav.cummax()
    dd = nav / peak - 1
    out, in_dd, start = [], False, None
    for d, v in dd.items():
        if v < 0 and not in_dd:
            in_dd, start = True, d
        elif v == 0 and in_dd:
            seg = dd.loc[start:d]
            out.append({"start": start, "trough": seg.idxmin(), "recovered": d,
                        "depth_pct": seg.min() * 100,
                        "months": (d - start).days / 30.44,
                        "months_to_trough": (seg.idxmin() - start).days / 30.44})
            in_dd = False
    if in_dd:
        seg = dd.loc[start:]
        out.append({"start": start, "trough": seg.idxmin(), "recovered": pd.NaT,
                    "depth_pct": seg.min() * 100,
                    "months": (dd.index[-1] - start).days / 30.44,
                    "months_to_trough": (seg.idxmin() - start).days / 30.44})
    df = pd.DataFrame(out)
    return df.sort_values("depth_pct").head(top) if not df.empty else df


def risk(nav: pd.Series, bench: pd.Series | None = None) -> dict:
    r = nav.pct_change().dropna()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1
    dd = (nav / nav.cummax() - 1)
    downside = r[r < 0]
    out = {
        "cagr_pct": cagr * 100,
        "volatility_pct": r.std() * np.sqrt(252) * 100,
        "sharpe_rf0": r.mean() / r.std() * np.sqrt(252),
        # Sortino punishes only downside movement, which is the risk that
        # matters to someone who has to live with the account.
        # Downside deviation is the root mean square of returns below zero
        # taken over ALL periods, not the standard deviation of the negative
        # days alone. The earlier version used the latter and understated
        # Sortino (1.60 where the standard definition gives 1.82); it now
        # agrees with quantstats.
        "sortino_rf0": (r.mean() / np.sqrt((np.minimum(r, 0) ** 2).mean()) * np.sqrt(252)
                        if len(downside) else np.nan),
        "max_drawdown_pct": dd.min() * 100,
        # Calmar: return per unit of worst loss.
        "calmar": cagr / abs(dd.min()) if dd.min() < 0 else np.nan,
        # Ulcer index: the average depth of being underwater, so long shallow
        # pain and one sharp crash score differently.
        "ulcer_index_pct": np.sqrt((dd ** 2).mean()) * 100,
        "worst_day_pct": r.min() * 100,
        "best_day_pct": r.max() * 100,
        "positive_days_pct": (r > 0).mean() * 100,
        "skew": float(r.skew()),
        "kurtosis_excess": float(r.kurt()),
        # 5% of days are worse than this, and when they are, this is the average.
        "var95_daily_pct": np.percentile(r, 5) * 100,
        "cvar95_daily_pct": r[r <= np.percentile(r, 5)].mean() * 100,
    }
    if bench is not None:
        b = bench.reindex(nav.index).ffill().pct_change().dropna()
        j = pd.concat([r.rename("s"), b.rename("b")], axis=1).dropna()
        cov = np.cov(j["s"], j["b"])
        out |= {
            "beta": cov[0, 1] / cov[1, 1],
            "correlation": float(j["s"].corr(j["b"])),
            "tracking_error_pct": (j["s"] - j["b"]).std() * np.sqrt(252) * 100,
            # Capture: in the months the market rose, did we rise more?
            "up_capture_pct": _capture(j, up=True),
            "down_capture_pct": _capture(j, up=False),
        }
    return out


def _capture(j: pd.DataFrame, up: bool) -> float:
    m = (1 + j).resample("ME").prod() - 1
    sel = m[m["b"] > 0] if up else m[m["b"] < 0]
    if sel.empty or sel["b"].mean() == 0:
        return float("nan")
    return sel["s"].mean() / sel["b"].mean() * 100


def rolling_excess(nav: pd.Series, bench: pd.Series, window: int = 252) -> dict:
    j = pd.concat([nav.rename("s"), bench.reindex(nav.index).ffill().rename("b")], axis=1).dropna()
    ex = (j["s"] / j["s"].shift(window) - 1) - (j["b"] / j["b"].shift(window) - 1)
    ex = ex.dropna()
    if ex.empty:
        return {}
    return {"windows": int(len(ex)), "beat_pct_of_time": float((ex > 0).mean() * 100),
            "median_excess_pct": float(ex.median() * 100),
            "worst_excess_pct": float(ex.min() * 100),
            "best_excess_pct": float(ex.max() * 100)}


def positions(episodes: pd.DataFrame, trades: pd.DataFrame) -> dict:
    """Per holding episode: how many worked, and how concentrated the wins are.

    The episode log carries an entry price but no exit price, so each episode is
    matched to its own buys and sells in the trade log. Rupees in against rupees
    out, net of the cost charged on both sides, which is what actually landed in
    the account rather than a paper price move.
    """
    if episodes.empty or trades.empty:
        return {}
    t = trades.copy()
    t["date"] = pd.to_datetime(t["date"])
    e = episodes.copy()
    e["entry"], e["exit"] = pd.to_datetime(e["entry"]), pd.to_datetime(e["exit"])
    # Positions still open on the last day have no sell leg, so a rupees-in
    # against rupees-out calculation would score them as a total loss. They are
    # counted separately rather than silently included.
    still_open = int((e["exit_reason"] == "open_at_end").sum())
    e = e[e["exit_reason"] != "open_at_end"]
    rows = []
    for _, ep in e.iterrows():
        leg = t[(t["symbol"] == ep["symbol"]) & (t["date"] >= ep["entry"])
                & (t["date"] <= ep["exit"])]
        if leg.empty:
            continue
        spent = (leg.loc[leg["side"] == "buy", "value"].sum()
                 + leg.loc[leg["side"] == "buy", "cost"].sum())
        got = (leg.loc[leg["side"] == "sell", "value"].sum()
               - leg.loc[leg["side"] == "sell", "cost"].sum())
        if spent <= 0:
            continue
        rows.append({"symbol": ep["symbol"], "pnl": got - spent, "ret": got / spent - 1,
                     "days": ep["days_held"], "doubled": bool(ep["doubled"]),
                     "reason": ep["exit_reason"]})
    if not rows:
        return {"episodes": int(len(e))}
    r = pd.DataFrame(rows)
    gross_win = r.loc[r["pnl"] > 0, "pnl"].sum()
    return {
        "closed_episodes": int(len(r)),
        "still_open_at_end": still_open,
        "win_rate_pct": float((r["ret"] > 0).mean() * 100),
        "median_return_pct": float(r["ret"].median() * 100),
        "mean_winner_pct": float(r.loc[r["ret"] > 0, "ret"].mean() * 100),
        "mean_loser_pct": float(r.loc[r["ret"] < 0, "ret"].mean() * 100),
        "best_pct": float(r["ret"].max() * 100), "worst_pct": float(r["ret"].min() * 100),
        "doubled_while_held": int(r["doubled"].sum()),
        "top5_share_of_gross_profit_pct": float(r.nlargest(5, "pnl")["pnl"].sum() / gross_win * 100)
        if gross_win > 0 else None,
        "median_holding_days": float(r["days"].median()),
        "max_holding_days": float(r["days"].max()),
        "exit_reasons": r["reason"].value_counts().to_dict(),
    }


def exposure(holdings: pd.DataFrame) -> dict:
    if holdings.empty or "sector" not in holdings:
        return {}
    h = holdings.copy()
    h["sector"] = h["sector"].fillna("(unlabelled)")
    share = h.groupby("sector").size() / len(h) * 100
    # An unlabelled company is its own sector under the rules, so counting the
    # unlabelled bucket as one sector would report a cap breach that never
    # happened.
    labelled = h[h["sector"] != "(unlabelled)"]
    per_date = (labelled.groupby([labelled.columns[0], "sector"]).size()
                .groupby(level=0).max() if not labelled.empty else pd.Series([0]))
    return {"share_of_all_slots_pct": share.sort_values(ascending=False).head(10).round(1).to_dict(),
            "max_names_in_one_labelled_sector": int(per_date.max()),
            "unlabelled_share_of_slots_pct": round(float((h["sector"] == "(unlabelled)").mean() * 100), 1),
            "distinct_stocks_ever_held": int(h["symbol"].nunique()) if "symbol" in h else None}


def main(argv=None) -> int:
    import argparse
    global RUN, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="data/backtest/v1_corrected")
    ap.add_argument("--out", default="data/backtest/stats_corrected")
    a = ap.parse_args(argv)
    RUN, OUT = Path(a.run), Path(a.out)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    OUT.mkdir(parents=True, exist_ok=True)
    nav = pd.read_csv(RUN / "nav.csv"); nav["date"] = pd.to_datetime(nav["date"])
    nav = nav.set_index("date")
    s = nav["strategy"].dropna()
    ew = nav["universe_ew"].dropna()
    tri = pd.read_parquet(TRI)
    n500 = tri[tri.index_name == "NIFTY 500"].set_index("date")["tri"].sort_index()

    rep = {"period": {"start": str(s.index[0].date()), "end": str(s.index[-1].date())}}
    rep["strategy"] = risk(s, n500)
    rep["universe_ew"] = risk(ew, n500)
    rep["nifty500_tri"] = risk(n500.loc[s.index[0]:s.index[-1]])
    rep["rolling_12m_vs_universe_ew"] = rolling_excess(s, ew)
    rep["rolling_12m_vs_nifty500_tri"] = rolling_excess(s, n500)
    dd = drawdowns(s)
    rep["worst_drawdowns"] = json.loads(dd.to_json(orient="records", date_format="iso")) if not dd.empty else []
    for name, f in (("positions", "episodes.csv"), ("exposure", "holdings.csv")):
        p = RUN / f
        if p.exists():
            df = pd.read_csv(p)
            rep[name] = (positions(df, pd.read_csv(RUN / "trades.csv"))
                         if name == "positions" else exposure(df))
    monthly = (s.resample("ME").last().pct_change().dropna() * 100).round(1)
    rep["monthly_pct"] = {str(k.date()): v for k, v in monthly.items()}
    (OUT / "stats_v1.json").write_text(json.dumps(rep, indent=2, default=float))

    def show(d, keys):
        return "  ".join(f"{k}={d[k]:.2f}" for k in keys if k in d and pd.notna(d[k]))

    print("\n=== RISK ===")
    for nm in ("strategy", "universe_ew", "nifty500_tri"):
        print(f" {nm:14}", show(rep[nm], ["cagr_pct", "volatility_pct", "sharpe_rf0", "sortino_rf0",
                                          "max_drawdown_pct", "calmar", "ulcer_index_pct"]))
    print("\n=== VS NIFTY 500 TOTAL RETURN ===")
    print(" strategy  ", show(rep["strategy"], ["beta", "correlation", "up_capture_pct", "down_capture_pct"]))
    print(" universe  ", show(rep["universe_ew"], ["beta", "correlation", "up_capture_pct", "down_capture_pct"]))
    print("\n=== TAIL RISK (daily) ===")
    print(" strategy  ", show(rep["strategy"], ["worst_day_pct", "var95_daily_pct", "cvar95_daily_pct",
                                                "positive_days_pct", "skew", "kurtosis_excess"]))
    print("\n=== ROLLING 12 MONTHS ===")
    for k in ("rolling_12m_vs_universe_ew", "rolling_12m_vs_nifty500_tri"):
        v = rep[k]
        if v:
            print(f" {k}: beats {v['beat_pct_of_time']:.0f}% of windows, median "
                  f"{v['median_excess_pct']:+.1f}pp, worst {v['worst_excess_pct']:+.1f}pp, "
                  f"best {v['best_excess_pct']:+.1f}pp")
    print("\n=== WORST DRAWDOWNS ===")
    if not dd.empty:
        d = dd.copy()
        for c in ("start", "trough", "recovered"):
            d[c] = pd.to_datetime(d[c]).dt.date
        print(d.round(1).to_string(index=False))
    print("\n=== POSITIONS ===");  print(" ", rep.get("positions"))
    print("\n=== EXPOSURE ===");   print(" ", rep.get("exposure"))
    print(f"\nwritten to {OUT}/stats_v1.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

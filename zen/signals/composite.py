"""Strategy v1 composite: eight measures, five groups, one equal-weight score.

Definitions are exactly those in research/strategy/v1-spec.md:

  Quality   margin       TTM EBITDA / TTM revenue
            stability    -sd(quarterly EBITDA margin), latest <= 8 quarters, needs >= 4
  Growth    rev_growth   revenue(L) / revenue(L-4) - 1
            profit_growth (profit(L) - profit(L-4)) / TTM revenue
  Value     earnings_yield  TTM normalised profit / market cap
            sales_yield  TTM revenue / market cap
  Momentum  mom_12_1     adj close 21 sessions before D / adj close 252 sessions before D - 1
  Risk      low_vol      -sd(daily adjusted log returns, last 252 sessions)

Each measure is a cross-sectional percentile within the universe at D, 1 best,
missing = 0.5. Group score = mean of its measures; composite = mean of the five
groups; ties broken by symbol.

The class `Composite` also satisfies the Strategy protocol in
zen/signals/base.py so zen/validation/leak.future_blindness can run it: it
emits one Signal per universe member with every measure, percentile and group
score in its facts, so any leak anywhere in the stack changes the fingerprint.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from zen.signals.base import Signal
from zen.universe import pit

log = logging.getLogger(__name__)

MEASURES = {
    "margin": "quality",
    "stability": "quality",
    "rev_growth": "growth",
    "profit_growth": "growth",
    "earnings_yield": "value",
    "sales_yield": "value",
    "mom_12_1": "momentum",
    "low_vol": "risk",
}
GROUPS = ["quality", "growth", "value", "momentum", "risk"]


def _price_measures(snap: pit.Snapshot, syms) -> pd.DataFrame:
    """Momentum 12-1 and low volatility from adjusted closes before D.

    Session k before D is the k-th market session counting back from the last
    one before D (k=1). A stock that did not trade on that session is valued
    at its last adjusted close on or before it.
    """
    cal = snap.cal
    s21, s252 = cal[-pit.MOM_SKIP], cal[-pit.MOM_LOOKBACK]
    px = snap.px[snap.px["symbol"].isin(syms)]

    prior = snap.px_prior
    prior = (prior[prior["symbol"].isin(syms)].set_index("symbol")["adj_close"]
             if len(prior) else pd.Series(dtype=float))

    def asof_close(t):
        sub = px[px["date"] <= t]
        # last close on or before t, however old (Clarification 2)
        return sub.groupby("symbol")["adj_close"].last().combine_first(prior)

    c21, c252 = asof_close(s21), asof_close(s252)
    mom = (c21 / c252 - 1).reindex(syms)

    win = px[px["date"] >= s252].sort_values(["symbol", "date"])
    lr = np.log(win["adj_close"]).groupby(win["symbol"]).diff()
    vol = lr.groupby(win["symbol"]).std(ddof=1).reindex(syms)
    return pd.DataFrame({"mom_12_1": mom, "low_vol": -vol,
                         "adj_close_s21": c21.reindex(syms),
                         "adj_close_s252": c252.reindex(syms)})


def measures(members: pd.DataFrame, fund: pd.DataFrame, snap: pit.Snapshot) -> pd.DataFrame:
    syms = members.index
    f = fund.reindex(syms)
    out = pd.DataFrame(index=syms)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["margin"] = (f["ttm_ebitda"] / f["ttm_revenue"]).where(f["ttm_revenue"] > 0)
        out["stability"] = -f["margin_sd"]
        out["rev_growth"] = (f["rev_latest"] / f["rev_yago"] - 1).where(f["rev_yago"] > 0)
        out["profit_growth"] = ((f["profit_latest"] - f["profit_yago"]) /
                                f["ttm_revenue"]).where(f["ttm_revenue"] > 0)
        out["earnings_yield"] = f["ttm_profit"] / members["mcap"]
        out["sales_yield"] = f["ttm_revenue"] / members["mcap"]
    out = out.join(_price_measures(snap, syms))
    out = out.replace([np.inf, -np.inf], np.nan)
    return out


def score(m: pd.DataFrame) -> pd.DataFrame:
    """Percentiles, group scores, composite, rank. Pure function of the measures."""
    out = m.copy()
    for k in MEASURES:
        out[f"pct_{k}"] = m[k].rank(method="average", pct=True).fillna(0.5)
    for g in GROUPS:
        cols = [f"pct_{k}" for k, gg in MEASURES.items() if gg == g]
        out[f"grp_{g}"] = out[cols].mean(axis=1)
    out["composite"] = out[[f"grp_{g}" for g in GROUPS]].mean(axis=1)
    # Leave-one-group-out composites (diagnostics).
    for g in GROUPS:
        others = [f"grp_{x}" for x in GROUPS if x != g]
        out[f"loo_{g}"] = out[others].mean(axis=1)
    # Rank 1 = best; ties by the symbol traded at D (ascending), then stock id.
    tick = out["ticker"] if "ticker" in out else pd.Series(out.index, index=out.index)
    order = (out.assign(_t=tick.to_numpy()).reset_index()
             .sort_values(["composite", "_t", "symbol"], ascending=[False, True, True])["symbol"])
    out["rank"] = pd.Series(np.arange(1, len(order) + 1), index=order.to_numpy())
    return out.sort_values("rank")


def compute(con, D, static: pit.StaticLabels | None = None):
    """Ranks and funnel at decision date D. The one code path for production and tests."""
    snap = pit.build_snapshot(con, D, static)
    members, funnel, fund = pit.universe(snap, static)
    m = measures(members, fund, snap)
    m["ticker"] = members["ticker"]
    ranked = score(m)
    f = fund.reindex(ranked.index)
    ranked = ranked.join(members[["mcap", "raw_close", "median_turnover_60",
                                  "sector", "sector_source"]])
    for c in ["consolidated", "latest_period_end", "consec_quarters", "ttm_revenue",
              "ttm_ebitda", "ttm_profit", "shares", "shares_filed", "shares_fix"]:
        ranked[c] = f[c]
    ranked.insert(0, "D", pd.Timestamp(D).normalize())
    # Clarification 30 audit trail: filings treated as not filed at D, and the
    # universe members whose share count was replaced.
    dq = snap.scale_dropped[["symbol", "consolidated", "period_end", "broadcast_dt",
                             "revenue", "shares_implied"]].assign(check="scale_error_dropped")
    sf = ranked.loc[ranked["shares_fix"].fillna("") != "",
                    ["shares_filed", "shares", "shares_fix"]].reset_index()
    sf = sf.rename(columns={"shares_fix": "check"})
    ranked.attrs["data_quality"] = pd.concat([dq, sf], ignore_index=True).assign(
        D=pd.Timestamp(D).normalize())
    return ranked, funnel


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "nan"
    if isinstance(v, (float, np.floating)):
        return f"{float(v):.10g}"
    return str(v)


class Composite:
    """The v1 composite as a Strategy (zen/signals/base.py) for the leak detector."""

    name = "v1_composite"
    min_history_days = 400
    validated = False

    FACT_COLS = (list(MEASURES) + [f"pct_{k}" for k in MEASURES] +
                 [f"grp_{g}" for g in GROUPS] +
                 ["composite", "rank", "mcap", "shares", "ttm_revenue", "ttm_ebitda",
                  "ttm_profit", "consolidated", "consec_quarters", "sector"])

    def __init__(self, static: pit.StaticLabels | None = None):
        self.static = static

    def generate(self, con, asof: date) -> list[Signal]:
        ranked, _ = compute(con, asof, self.static)
        sigs = []
        for sym, r in ranked.iterrows():
            sigs.append(Signal(
                symbol=sym, action="buy", asof=asof, strategy=self.name,
                conviction=float(r["composite"]),
                facts={c: _fmt(r[c]) for c in self.FACT_COLS},
                rationale=[f"Composite rank {int(r['rank'])} of {len(ranked)}."],
                against=["A cross-sectional rank, not a forecast: the composite has "
                         "not yet been shown to carry signal out of sample."],
            ))
        return sigs

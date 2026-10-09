"""Valuation history: the trailing P/E a company traded at, month by month, point in time.

For each month-end session the P/E is the closing price over the sum of the last four quarters'
basic EPS, using only the results filed by the end of that day (the latest revision of each
quarter known then). Both sides are on today's share basis: prices are split-adjusted, and each
quarter's EPS is restated for every split or bonus that went ex after it was filed. The same
guards as the research pages' current P/E apply, and a month that fails one is left empty
rather than filled with a wrong number:

  * the last four quarters must be consecutive, and the latest no older than STALE_DAYS;
  * trailing EPS under MIN_EPS gives no P/E (a loss, or a figure too small to divide by);
  * the share count implied by each quarter (profit over restated EPS) must agree across the
    four within SHARE_BAND, or the EPS figures are on different share bases;
  * no unexplained jump in the adjusted price may fall inside the window, since the price and
    the EPS would then describe different businesses.

`pe_series` is pure (frames in, a list out) so the research export and the MCP server compute
exactly the same thing. Nothing in zen's strategies calls it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_EPS = 0.25
STALE_DAYS = 200
SHARE_BAND = 1.6                # largest over smallest implied share count across the four quarters
BREAK_DAILY, BREAK_WEEKLY = 1.6, 2.6


def price_breaks(px: pd.DataFrame) -> list[pd.Timestamp]:
    """Dates where the adjusted close steps further than any market move could (the research
    pages' rule: 1.6x between daily closes, 2.6x between weekly ones)."""
    v = px["adj"].to_numpy(dtype=float)
    d = pd.to_datetime(px["date"]).reset_index(drop=True)
    out = []
    for i in range(1, len(v)):
        if not (v[i] > 0 and v[i - 1] > 0):
            continue
        r = v[i] / v[i - 1]
        lim = BREAK_DAILY if (d.iloc[i] - d.iloc[i - 1]).days <= 4 else BREAK_WEEKLY
        if r > lim or r < 1 / lim:
            out.append(d.iloc[i])
    return out


def _restate(eps: float, filed: pd.Timestamp, fac: pd.DataFrame) -> float:
    if fac is None or not len(fac):
        return eps
    later = fac.loc[fac["ex_date"] > filed, "factor"]
    return eps * (float(np.prod(later.to_numpy(dtype=float))) if len(later) else 1.0)


def pe_series(fil: pd.DataFrame, fac: pd.DataFrame | None, px: pd.DataFrame, brk=(), months: int = 60,
              asof=None) -> list[dict]:
    """[{d, pe}] oldest first, one per month-end session in the last `months` months.

    fil: one basis' quarterly filings, every revision: period_end, broadcast_dt, eps, profit.
    fac: ex_date, factor (price multipliers of splits and bonuses). px: date, adj (closes on
    today's share basis), ascending. brk: dates of unexplained price jumps."""
    if px is None or not len(px):
        return []
    px = px.assign(date=pd.to_datetime(px["date"])).sort_values("date")
    end = pd.Timestamp(asof) if asof is not None else px["date"].iloc[-1]
    start = end - pd.DateOffset(months=months)
    p = px[(px["date"] > start) & (px["date"] <= end)]
    if not len(p):
        return []
    me = p.groupby(p["date"].dt.to_period("M")).tail(1)
    f = fil.dropna(subset=["eps"]).copy()
    if fac is not None and len(fac):
        fac = fac.assign(ex_date=pd.to_datetime(fac["ex_date"]))
    if len(f):
        f["period_end"] = pd.to_datetime(f["period_end"])
        f["broadcast_dt"] = pd.to_datetime(f["broadcast_dt"])
        f = f.sort_values("broadcast_dt")
        f["eps_r"] = [_restate(float(e), b, fac) for e, b in zip(f["eps"], f["broadcast_dt"])]
    brk = sorted(pd.Timestamp(b) for b in brk)
    out = []
    for d, price in zip(me["date"], me["adj"]):
        pe = None
        known = f[f["broadcast_dt"] < d + pd.Timedelta(days=1)] if len(f) else f
        if len(known):
            latest = known.drop_duplicates("period_end", keep="last").sort_values("period_end")
            last4 = latest.tail(4)
            qn = (last4["period_end"].dt.year * 4 + (last4["period_end"].dt.month - 1) // 3).tolist()
            ok = (len(last4) == 4 and qn == list(range(qn[0], qn[0] + 4))
                  and (d - last4["period_end"].max()).days <= STALE_DAYS)
            if ok:
                ttm = float(last4["eps_r"].sum())
                implied = [pr / e for pr, e in zip(last4["profit"], last4["eps_r"])
                           if pd.notna(pr) and abs(e) >= 0.2 and (pr > 0) == (e > 0)]
                if len(implied) >= 2 and max(implied) / min(implied) > SHARE_BAND:
                    ok = False
                window_start = last4["period_end"].min() - pd.DateOffset(months=3)
                if any(window_start < b <= d for b in brk):
                    ok = False
                if ok and ttm >= MIN_EPS and price > 0:
                    pe = round(float(price) / ttm, 1)
        out.append({"d": d.strftime("%Y-%m-%d"), "pe": pe})
    return out


def summary(series: list[dict], min_points: int = 12) -> dict | None:
    """Median, low, high and the latest value of a P/E series, and where the latest sits in it
    (the share of months at or below it). None with fewer than `min_points` values."""
    vals = [x["pe"] for x in series if x["pe"] is not None]
    if len(vals) < min_points:
        return None
    cur = next((x for x in reversed(series) if x["pe"] is not None), None)
    a = np.array(vals, dtype=float)
    return {"median": round(float(np.median(a)), 1), "low": round(float(a.min()), 1), "high": round(float(a.max()), 1),
            "latest": cur["pe"], "latest_date": cur["d"], "n": len(vals),
            "pct_at_or_below": round(float((a <= cur["pe"]).mean()), 3)}

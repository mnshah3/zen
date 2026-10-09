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


def _later_factors(filed: np.ndarray, fac: pd.DataFrame | None) -> np.ndarray:
    """For each filing time, the product of the split and bonus factors that went ex after it."""
    if fac is None or not len(fac):
        return np.ones(len(filed))
    f = fac.assign(ex_date=pd.to_datetime(fac["ex_date"])).sort_values("ex_date")
    ex = f["ex_date"].to_numpy(dtype="datetime64[ns]")
    suffix = np.append(np.cumprod(f["factor"].to_numpy(dtype=float)[::-1])[::-1], 1.0)   # product from i to the end
    return suffix[np.searchsorted(ex, filed, side="right")]


def pe_series(fil: pd.DataFrame, fac: pd.DataFrame | None, px: pd.DataFrame, brk=(), months: int = 60,
              asof=None) -> list[dict]:
    """[{d, pe}] oldest first, one per month-end session in the last `months` months.

    fil: one basis' quarterly filings, every revision: period_end, broadcast_dt, eps, profit, and
    xbrl_url where known (it orders revisions broadcast at the same instant).
    fac: ex_date, factor (price multipliers of splits and bonuses). px: date, adj (closes on
    today's share basis), ascending. brk: dates of unexplained price jumps.

    The filings are walked once in the order they were broadcast, keeping the latest revision of
    each quarter, so each month-end sees exactly what had been filed by the end of that day."""
    if px is None or not len(px):
        return []
    px = px.assign(date=pd.to_datetime(px["date"])).sort_values("date")
    end = pd.Timestamp(asof) if asof is not None else px["date"].iloc[-1]
    start = end - pd.DateOffset(months=months)
    p = px[(px["date"] > start) & (px["date"] <= end)]
    if not len(p):
        return []
    me = p.groupby(p["date"].dt.to_period("M")).tail(1)
    f = fil.dropna(subset=["eps"])
    f = f.assign(period_end=pd.to_datetime(f["period_end"]), broadcast_dt=pd.to_datetime(f["broadcast_dt"]))
    # revisions broadcast at the same instant: NSE's document link, whose filing number rises with
    # each upload, decides which is the later one (the database returns rows in no fixed order)
    keys = ["broadcast_dt", "xbrl_url"] if "xbrl_url" in f else ["broadcast_dt"]
    f = f.sort_values(keys, kind="stable")
    filed = f["broadcast_dt"].to_numpy(dtype="datetime64[ns]")
    eps_r = f["eps"].to_numpy(dtype=float) * _later_factors(filed, fac)
    pend = list(f["period_end"])
    prof = f["profit"].to_numpy(dtype=float) if "profit" in f else np.full(len(f), np.nan)
    brk = sorted(pd.Timestamp(b) for b in brk)
    latest: dict = {}                       # period_end -> (restated eps, profit), latest revision so far
    k, out = 0, []
    for d, price in zip(me["date"], me["adj"]):
        cutoff = np.datetime64(d + pd.Timedelta(days=1), "ns")
        while k < len(filed) and filed[k] < cutoff:
            latest[pend[k]] = (eps_r[k], prof[k])
            k += 1
        pe = None
        if len(latest) >= 4:
            q4 = sorted(latest)[-4:]
            qn = [x.year * 4 + (x.month - 1) // 3 for x in q4]
            ok = qn == list(range(qn[0], qn[0] + 4)) and (d - q4[-1]).days <= STALE_DAYS
            if ok:
                vals = [latest[x] for x in q4]
                ttm = float(sum(v[0] for v in vals))
                implied = [pr / e for e, pr in vals if not np.isnan(pr) and abs(e) >= 0.2 and (pr > 0) == (e > 0)]
                if len(implied) >= 2 and max(implied) / min(implied) > SHARE_BAND:
                    ok = False
                window_start = q4[0] - pd.DateOffset(months=3)
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

"""Market statistics computed from our own archive, not scraped from a site.

Breadth is more informative than an index level: an index can rise while most
stocks fall, and that divergence is usually the interesting part.

Note on size tiers: real market cap needs shares outstanding, which we do not
have yet. Until then stocks are bucketed by traded turnover, which is a
liquidity proxy -- correlated with size but not the same thing. Labelled
honestly so nobody later mistakes it for market cap.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from zen.data import store

log = logging.getLogger(__name__)


def latest_session(con) -> date | None:
    row = con.execute("SELECT max(date) FROM prices").fetchone()
    return row[0] if row and row[0] else None


def breadth(con, d: date) -> dict:
    """Advance/decline and participation for one session."""
    df = con.execute(
        """
        SELECT symbol, close, prev_close, turnover, volume
        FROM prices
        WHERE date = ? AND isin_code LIKE 'INE%' AND prev_close > 0
        """,
        [d],
    ).df()
    if df.empty:
        return {}

    df["ret"] = df["close"] / df["prev_close"] - 1
    adv = int((df["ret"] > 0).sum())
    dec = int((df["ret"] < 0).sum())
    unch = int((df["ret"] == 0).sum())

    return {
        "date": d,
        "traded": len(df),
        "advancers": adv,
        "decliners": dec,
        "unchanged": unch,
        "ad_ratio": round(adv / dec, 2) if dec else None,
        "median_ret": round(float(df["ret"].median()) * 100, 2),
        "pct_up_2": round(float((df["ret"] > 0.02).mean()) * 100, 1),
        "pct_down_2": round(float((df["ret"] < -0.02).mean()) * 100, 1),
        "turnover_cr": round(float(df["turnover"].sum()) / 1e7, 0),
    }


def tier_performance(con, d: date, tiers: int = 3) -> pd.DataFrame:
    """Median return by turnover tier -- a rough large/mid/small read."""
    df = con.execute(
        """
        SELECT symbol, close, prev_close, turnover
        FROM prices
        WHERE date = ? AND isin_code LIKE 'INE%' AND prev_close > 0 AND turnover > 0
        """,
        [d],
    ).df()
    if len(df) < tiers * 10:
        return pd.DataFrame()

    df["ret"] = (df["close"] / df["prev_close"] - 1) * 100
    labels = ["High turnover (large-ish)", "Mid turnover", "Low turnover (small-ish)"][:tiers]
    df["tier"] = pd.qcut(df["turnover"].rank(method="first", ascending=False),
                         tiers, labels=labels)
    out = (df.groupby("tier", observed=True)
             .agg(stocks=("symbol", "size"), median_ret=("ret", "median"))
             .reset_index())
    out["median_ret"] = out["median_ret"].round(2)
    return out


def movers(con, d: date, n: int = 5, min_turnover_cr: float = 5.0) -> dict:
    """Biggest movers, filtered for liquidity so illiquid noise stays out."""
    df = con.execute(
        """
        SELECT symbol, close, prev_close, turnover
        FROM prices
        WHERE date = ? AND isin_code LIKE 'INE%' AND prev_close > 0
          AND turnover >= ?
        """,
        [d, min_turnover_cr * 1e7],
    ).df()
    if df.empty:
        return {"gainers": [], "losers": []}

    df["ret"] = (df["close"] / df["prev_close"] - 1) * 100
    df["turnover_cr"] = (df["turnover"] / 1e7).round(1)
    cols = ["symbol", "close", "ret", "turnover_cr"]
    return {
        "gainers": df.nlargest(n, "ret")[cols].round(2).to_dict("records"),
        "losers": df.nsmallest(n, "ret")[cols].round(2).to_dict("records"),
    }


def summary(db_path=None) -> dict:
    """Everything the daily brief needs from the archive."""
    con = store.connect(db_path) if db_path else store.connect()
    try:
        d = latest_session(con)
        if d is None:
            return {}
        return {
            "session": d,
            "breadth": breadth(con, d),
            "tiers": tier_performance(con, d),
            "movers": movers(con, d),
            "coverage": store.coverage(con),
        }
    finally:
        con.close()

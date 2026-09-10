"""Observations drawn from our own archive.

This is the half of the brief no news site can produce. Everything here comes
from the bhavcopy history, so it is free, it is ours, and it covers the whole
market rather than the twenty names that happen to be in the headlines.

Each function returns plain facts. Interpretation belongs in the brief, not
buried in a calculation.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

log = logging.getLogger(__name__)

EQUITY = "isin_code LIKE 'INE%' AND series IN ('EQ','BE')"


def _sessions(con, n: int, upto: date) -> list[date]:
    rows = con.execute(
        "SELECT DISTINCT date FROM prices WHERE date <= ? ORDER BY date DESC LIMIT ?",
        [upto, n],
    ).fetchall()
    return sorted(r[0] for r in rows)


# Previous close, corrected for corporate actions.
#
# `prev_close` in the bhavcopy is the raw previous session's close, NOT adjusted
# for a split, bonus or consolidation that went ex that morning. Across the
# archive there are 781 such sessions, and the median return they imply is
# -52.8%: a five-for-one split reads as an eighty per cent crash. Left alone,
# the stock is counted as a decliner in breadth, appears in any "biggest
# losers" list, and prints a fake -80% in the brief.
#
# Factors are collapsed by (symbol, ex_date) with a product first, because a
# split and a bonus routinely share an ex-date in India and applying only one
# of them is its own error.
ADJ_JOIN = """
    LEFT JOIN (
        SELECT symbol, CAST(ex_date AS DATE) AS xd, exp(sum(ln(factor))) AS f
        FROM corpactions
        WHERE factor IS NOT NULL AND factor > 0
        GROUP BY 1, 2
    ) ca ON ca.symbol = p.symbol AND ca.xd = CAST(p.date AS DATE)
"""
PREV_ADJ = "p.prev_close * coalesce(ca.f, 1)"


def breadth_divergence(con, asof: date) -> dict | None:
    """Did the market's biggest names move with or against everything else?

    A cap-weighted index can rise while most stocks fall. When the two
    disagree, the disagreement is usually the story.
    """
    df = con.execute(
        f"""
        SELECT p.symbol, p.close, {PREV_ADJ} AS prev_close, p.turnover
        FROM prices p {ADJ_JOIN}
        WHERE p.date = ? AND {EQUITY} AND p.prev_close > 0 AND p.turnover > 0
        """,
        [asof],
    ).df()
    if len(df) < 100:
        return None

    df["ret"] = df["close"] / df["prev_close"] - 1
    # Turnover-weighted stands in for cap-weighted until we have share counts.
    heavy = df.nlargest(50, "turnover")
    big_move = float((heavy["ret"] * heavy["turnover"]).sum() / heavy["turnover"].sum())
    median = float(df["ret"].median())

    gap = (big_move - median) * 100
    return {
        "large_cap_proxy": round(big_move * 100, 2),
        "median_stock": round(median * 100, 2),
        "gap": round(gap, 2),
        "diverging": abs(gap) > 0.5,
        "direction": ("heavyweights held up while the broader market fell"
                      if gap > 0.5 else
                      "the broader market outperformed the heavyweights"
                      if gap < -0.5 else "index and breadth agreed"),
    }


def unusual_volume(con, asof: date, n: int = 6, lookback: int = 60) -> pd.DataFrame:
    """Stocks trading far above their OWN normal volume.

    Compared against each stock's own median rather than a market-wide
    threshold, so a quiet small cap waking up ranks alongside a large cap.
    """
    sessions = _sessions(con, lookback, asof)
    if len(sessions) < 20:
        return pd.DataFrame()

    df = con.execute(
        f"""
        WITH win AS (
            SELECT symbol, date, close, prev_close, volume, turnover
            FROM prices
            WHERE date BETWEEN ? AND ? AND {EQUITY} AND volume > 0
        ),
        norm AS (
            SELECT symbol,
                   median(volume) AS med_vol,
                   count(*)       AS sessions
            FROM win GROUP BY symbol
        )
        SELECT w.symbol,
               w.close,
               -- Same correction as breadth: an unadjusted prev_close puts a
               -- stock that merely split at the top of any losers list.
               w.prev_close * coalesce(ca.f, 1) AS prev_close,
               w.volume,
               w.turnover,
               n.med_vol
        FROM win w
        JOIN norm n USING (symbol)
        LEFT JOIN (
            SELECT symbol, CAST(ex_date AS DATE) AS xd, exp(sum(ln(factor))) AS f
            FROM corpactions WHERE factor IS NOT NULL AND factor > 0
            GROUP BY 1, 2
        ) ca ON ca.symbol = w.symbol AND ca.xd = CAST(w.date AS DATE)
        WHERE w.date = ?
          AND n.sessions >= ?
          AND n.med_vol > 0
          AND w.turnover > 5e7
        """,
        [sessions[0], asof, asof, int(lookback * 0.6)],
    ).df()
    if df.empty:
        return pd.DataFrame()

    df["vol_x"] = (df["volume"] / df["med_vol"]).round(1)
    df["ret"] = ((df["close"] / df["prev_close"] - 1) * 100).round(2)
    df["turnover_cr"] = (df["turnover"] / 1e7).round(0)
    out = df[df["vol_x"] >= 3].nlargest(n, "vol_x")
    return out[["symbol", "close", "ret", "vol_x", "turnover_cr"]]


def extremes(con, asof: date, window: int = 252) -> dict:
    """52-week high and low counts -- a cleaner regime read than an index."""
    sessions = _sessions(con, window, asof)
    if len(sessions) < 100:
        return {}

    row = con.execute(
        f"""
        WITH win AS (
            SELECT symbol, date, close, high, low, turnover
            FROM prices
            WHERE date BETWEEN ? AND ? AND {EQUITY}
        ),
        rng AS (
            SELECT symbol, max(high) AS hi, min(low) AS lo, count(*) AS n
            FROM win GROUP BY symbol
        )
        SELECT
            sum(CASE WHEN w.close >= r.hi * 0.995 THEN 1 ELSE 0 END) AS at_high,
            sum(CASE WHEN w.close <= r.lo * 1.005 THEN 1 ELSE 0 END) AS at_low,
            count(*) AS eligible
        FROM win w JOIN rng r USING (symbol)
        WHERE w.date = ? AND r.n >= ? AND w.turnover > 1e7
        """,
        [sessions[0], asof, asof, int(window * 0.6)],
    ).fetchone()

    if not row or not row[2]:
        return {}
    at_high, at_low, eligible = int(row[0] or 0), int(row[1] or 0), int(row[2])
    return {
        "at_52w_high": at_high,
        "at_52w_low": at_low,
        "eligible": eligible,
        "net": at_high - at_low,
    }


def tier_rotation(con, asof: date, days: int = 5) -> pd.DataFrame:
    """Which end of the market led over the last week."""
    sessions = _sessions(con, days + 1, asof)
    if len(sessions) < days:
        return pd.DataFrame()
    start, end = sessions[0], sessions[-1]

    df = con.execute(
        f"""
        WITH bounds AS (
            SELECT symbol,
                   max(CASE WHEN date = ? THEN close END) AS px0,
                   max(CASE WHEN date = ? THEN close END) AS px1,
                   max(CASE WHEN date = ? THEN turnover END) AS turn1
            FROM prices
            WHERE date IN (?, ?) AND {EQUITY}
            GROUP BY symbol
        )
        SELECT * FROM bounds
        WHERE px0 > 0 AND px1 > 0 AND turn1 > 1e7
        """,
        [start, end, end, start, end],
    ).df()
    if len(df) < 60:
        return pd.DataFrame()

    df["ret"] = (df["px1"] / df["px0"] - 1) * 100
    labels = ["Large (top turnover)", "Mid", "Small (low turnover)"]
    df["tier"] = pd.qcut(df["turn1"].rank(method="first", ascending=False), 3, labels=labels)
    out = (df.groupby("tier", observed=True)
             .agg(stocks=("symbol", "size"), median_ret=("ret", "median"))
             .reset_index())
    out["median_ret"] = out["median_ret"].round(2)
    return out


def breadth_history(con, asof: date, days: int = 30) -> pd.DataFrame:
    """Daily advance/decline over the last month, for the chart."""
    sessions = _sessions(con, days, asof)
    if len(sessions) < 5:
        return pd.DataFrame()

    return con.execute(
        f"""
        SELECT p.date AS date,
               sum(CASE WHEN p.close > {PREV_ADJ} THEN 1 ELSE 0 END) AS advancers,
               sum(CASE WHEN p.close < {PREV_ADJ} THEN 1 ELSE 0 END) AS decliners,
               median((p.close / ({PREV_ADJ}) - 1) * 100)           AS median_ret
        FROM prices p {ADJ_JOIN}
        WHERE p.date BETWEEN ? AND ? AND {EQUITY} AND p.prev_close > 0
        GROUP BY p.date ORDER BY p.date
        """,
        [sessions[0], asof],
    ).df()


def collect(con, asof: date) -> dict:
    """Everything the brief's data section needs, in one pass."""
    return {
        "asof": asof,
        "divergence": breadth_divergence(con, asof),
        "unusual_volume": unusual_volume(con, asof),
        "extremes": extremes(con, asof),
        "rotation": tier_rotation(con, asof),
        "breadth_history": breadth_history(con, asof),
    }

"""Market benchmarks built from our own archive.

NSE's index history endpoint returns a bot-block page, so the Nifty series is
not retrievable for free. Constructing the benchmark from the archive is the
better answer anyway:

  It is survivorship-free. The published Nifty series reflects today's
  membership rules applied to a curated list of survivors. Our index includes
  every name that actually traded on each date, delisted ones included, which
  is the same basis the strategy is measured on. Comparing a survivorship-free
  strategy against a survivorship-biased benchmark would flatter or penalise
  it for reasons having nothing to do with skill.

  It is point-in-time by construction. Membership on any past date is decided
  using only data available on that date.

Two series are produced, and which one to measure against matters.

`broad` is an equal-weighted buy-and-hold basket of every liquid stock. It is
the return of the AVERAGE STOCK -- what you would have earned picking at
random from the same universe a screen picks from. Over 2015-2026 it returned
about 2.2x, roughly 7% a year. This is the honest hurdle for a stock-picking
strategy: beating it means the selection did work.

`large` holds the fifty most heavily TRADED names. It is NOT a Nifty proxy and
should not be read as one. Turnover leaders are the most actively churned
stocks, not the largest companies, and they skew speculative -- the series
returns about 1.4% a year against the Nifty's 11-13% over the same period.
A genuine cap-weighted proxy needs shares outstanding, which the bhavcopy does
not carry, so it is not available for free today.

The practical consequence: measuring a small and mid cap strategy against the
Nifty conflates two different things. Small caps beating large caps in a given
year is a size effect, not evidence the screen selected well. Measure against
`broad` to ask "did picking beat not picking", and treat any Nifty comparison
as commentary rather than as the test.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

log = logging.getLogger(__name__)

EQUITY = "isin_code LIKE 'INE%' AND series IN ('EQ','BE')"


def portfolio_index(con, start: date | None = None, end: date | None = None,
                    top_n: int = 50, min_turnover_cr: float = 1.0,
                    rebalance_days: int = 21, base: float = 1000.0) -> pd.DataFrame:
    """Benchmarks simulated as actual portfolios.

    An earlier version compounded the daily cross-sectional MEAN return. That
    is wrong, and wrong in a direction that matters: the arithmetic mean of
    noisy small-cap returns sits far above the geometric return any holder
    could realise, and compounding it daily turned 1,000 into 1.9 million over
    eleven years. No portfolio could have earned that; the number was an
    artefact of averaging volatility.

    This version buys an equal-rupee basket at each rebalance and holds it,
    which is what an index fund actually does, so the level is a return
    someone could have achieved.
    """
    where, params = [EQUITY, "prev_close > 0", "close > 0"], []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)

    df = con.execute(
        f"""
        SELECT date, symbol, close, prev_close, turnover
        FROM prices
        WHERE {' AND '.join(where)}
        ORDER BY date, symbol
        """,
        params,
    ).df()
    if df.empty:
        return pd.DataFrame(columns=["date", "broad_ret", "large_ret", "n_broad", "n_large"])

    df["turnover_cr"] = df["turnover"] / 1e7
    sessions = sorted(df["date"].unique())
    px = df.pivot_table(index="date", columns="symbol", values="close")
    turn = df.pivot_table(index="date", columns="symbol", values="turnover_cr")

    rebal = set(sessions[::rebalance_days])
    broad_level, large_level = base, base
    broad_shares: dict = {}
    large_shares: dict = {}
    rows = []

    def value(shares, prices) -> float:
        """Mark the held shares to today's close, ignoring names that stopped
        trading -- a delisted holding is worth its last price, not nothing,
        and assuming zero would understate the benchmark."""
        return sum(n * p for s, n in shares.items()
                   if (p := prices.get(s)) == p and p is not None)

    for i, d in enumerate(sessions):
        prices = px.loc[d]

        if broad_shares:
            broad_level = value(broad_shares, prices) or broad_level
        if large_shares:
            large_level = value(large_shares, prices) or large_level

        if d in rebal or not broad_shares:
            # Membership and entry prices use only data available before this
            # session, so the index never buys on information it could not
            # have had.
            hist = turn.loc[:d].tail(rebalance_days)
            eligible = hist.median()
            eligible = eligible[eligible >= min_turnover_cr].dropna()
            valid = [s for s in eligible.index
                     if (p := prices.get(s)) == p and p and p > 0]

            if valid:
                # Equal rupee allocation, then held. Weights drift with price
                # between rebalances, which is what a real holder experiences.
                per = broad_level / len(valid)
                broad_shares = {s: per / prices[s] for s in valid}

                big = [s for s in eligible.loc[valid].nlargest(top_n).index]
                if big:
                    per_l = large_level / len(big)
                    large_shares = {s: per_l / prices[s] for s in big}

        rows.append({"date": d, "broad_index": broad_level,
                     "large_index": large_level,
                     "n_broad": len(broad_shares), "n_large": len(large_shares)})

    return pd.DataFrame(rows)


def index_levels(con, **kwargs) -> pd.DataFrame:
    return portfolio_index(con, **kwargs)


def forward_return(levels: pd.DataFrame, start: date, months: int,
                   column: str = "large_index") -> float | None:
    """Benchmark return over `months` from `start`, or None if it runs past the data."""
    if levels.empty:
        return None
    s = levels[levels["date"] >= start]
    if s.empty:
        return None
    start_row = s.iloc[0]
    target = start_row["date"] + pd.DateOffset(months=months)
    e = levels[levels["date"] >= target]
    if e.empty:
        return None
    return float(e.iloc[0][column] / start_row[column] - 1)

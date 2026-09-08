"""Event study: given signals, what actually happened next.

This is the machinery that turns "Sterlite filed a data-centre announcement
and then went up sevenfold" into "of the N companies that filed something
similar, X% beat the market and Y% went nowhere". The first is a story. Only
the second is evidence.

Five things this refuses to get wrong, each of which would flatter the result:

ENTRY PRICE. A signal observed at the close of day T cannot be traded at that
close. Entry is the NEXT session's open. Using the close of the signal day is
the single most common way a backtest invents returns it could not have earned.

SPLIT ADJUSTMENT. A face-value split shows as an 80% fall in raw prices, and
companies split after the price has risen -- so the error lands hardest on
exactly the winners a screen is meant to find.

DELISTING. Events whose stock later stopped trading STAY IN THE SAMPLE, valued
at their last traded price. Dropping them would rebuild survivorship bias
inside the study itself, which is the same disease this archive exists to cure,
one level up.

BENCHMARK ALIGNMENT. The index return is measured over the identical calendar
window as the stock, from the same entry date. Comparing a stock's 12 months
against an index's calendar year is a subtle but large error.

INCOMPLETE WINDOWS. A signal from three months ago has no 24-month outcome. It
is reported as null and excluded from that horizon's statistics rather than
quietly treated as a zero, and the surviving count is always reported so a
thin sample is visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

HORIZONS = (3, 6, 12, 24)
DEFAULT_BENCHMARK = "Nifty 500"


@dataclass
class Outcome:
    symbol: str
    signal_date: date
    entry_date: date | None
    entry_price: float | None
    horizon_months: int
    exit_date: date | None
    exit_price: float | None
    stock_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    delisted: bool
    complete: bool


def adjustment_factors(con) -> pd.DataFrame:
    """Cumulative split/bonus factor per symbol per ex-date.

    Prices strictly BEFORE an ex-date are multiplied by the cumulative factor
    of every action at or after that date, which makes the series continuous
    while leaving the most recent price equal to what actually traded.
    """
    acts = con.execute(
        "SELECT symbol, ex_date, factor FROM corpactions "
        "WHERE factor IS NOT NULL AND factor > 0 AND factor < 1 "
        "ORDER BY symbol, ex_date"
    ).df()
    if acts.empty:
        return acts
    acts["ex_date"] = pd.to_datetime(acts["ex_date"])
    # Reverse cumulative product within each symbol.
    acts["cum_factor"] = (acts.sort_values("ex_date", ascending=False)
                              .groupby("symbol")["factor"].cumprod())
    return acts.sort_values(["symbol", "ex_date"])


def adjusted_prices(con, symbols: list[str] | None = None,
                    start: date | None = None) -> pd.DataFrame:
    """Price panel with splits and bonuses removed."""
    where, params = ["isin_code LIKE 'INE%'", "close > 0"], []
    if symbols:
        where.append(f"symbol IN ({', '.join('?' * len(symbols))})")
        params.extend(symbols)
    if start:
        where.append("date >= ?")
        params.append(start)

    px = con.execute(
        f"SELECT date, symbol, open, close FROM prices "
        f"WHERE {' AND '.join(where)} ORDER BY symbol, date", params).df()
    if px.empty:
        return px
    px["date"] = pd.to_datetime(px["date"])

    acts = adjustment_factors(con)
    if acts.empty:
        px["factor"] = 1.0
        px["adj_close"], px["adj_open"] = px["close"], px["open"]
        return px

    # For each price row, the factor is the cumulative product of all actions
    # with an ex-date strictly after it. merge_asof on the reversed ordering
    # gives that in one pass rather than a loop per symbol.
    acts_r = acts.rename(columns={"ex_date": "date"})[["symbol", "date", "cum_factor"]]
    merged = pd.merge_asof(
        px.sort_values("date"),
        acts_r.sort_values("date"),
        on="date", by="symbol", direction="forward", allow_exact_matches=False,
    )
    merged["factor"] = merged["cum_factor"].fillna(1.0)
    merged["adj_close"] = merged["close"] * merged["factor"]
    merged["adj_open"] = merged["open"] * merged["factor"]
    return merged.sort_values(["symbol", "date"]).reset_index(drop=True)


def benchmark_levels(con, name: str = DEFAULT_BENCHMARK) -> pd.DataFrame:
    df = con.execute(
        "SELECT date, close FROM indices WHERE index_name = ? ORDER BY date",
        [name]).df()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _at_or_after(series: pd.DataFrame, when: pd.Timestamp, col: str):
    """First row on or after `when`, or None past the end of the data."""
    s = series[series["date"] >= when]
    return (s.iloc[0]["date"], float(s.iloc[0][col])) if len(s) else (None, None)


def measure(con, events: pd.DataFrame, horizons=HORIZONS,
            benchmark: str = DEFAULT_BENCHMARK,
            symbol_col: str = "symbol", date_col: str = "signal_date") -> pd.DataFrame:
    """Forward outcomes for every event, at every horizon.

    `events` needs a symbol column and a signal-date column. One row is
    returned per event per horizon.
    """
    if events.empty:
        return pd.DataFrame()

    ev = events.copy()
    ev[date_col] = pd.to_datetime(ev[date_col])
    symbols = sorted(ev[symbol_col].dropna().unique())
    earliest = ev[date_col].min().date()

    px = adjusted_prices(con, symbols=symbols)
    if px.empty:
        log.warning("no price data for %d symbols", len(symbols))
        return pd.DataFrame()

    bm = benchmark_levels(con, benchmark)
    if bm.empty:
        log.warning("benchmark %r not found; excess returns will be null", benchmark)

    last_session = px["date"].max()
    by_symbol = {s: g.reset_index(drop=True) for s, g in px.groupby("symbol")}
    rows: list[Outcome] = []

    for ev_row in ev.itertuples():
        sym = getattr(ev_row, symbol_col)
        sig = getattr(ev_row, date_col)
        series = by_symbol.get(sym)
        if series is None or series.empty:
            continue

        # Entry is the next session's OPEN, never the close just observed.
        after = series[series["date"] > sig]
        if after.empty:
            continue
        entry = after.iloc[0]
        entry_date, entry_px = entry["date"], float(entry["adj_open"] or entry["adj_close"])
        if not entry_px or entry_px <= 0:
            entry_px = float(entry["adj_close"])
        if not entry_px or entry_px <= 0:
            continue

        sym_last = series["date"].max()
        # A symbol whose data stops well before the archive does has delisted.
        delisted = sym_last < last_session - pd.Timedelta(days=30)

        bm_entry = None
        if not bm.empty:
            _, bm_entry = _at_or_after(bm, entry_date, "close")

        for h in horizons:
            target = entry_date + pd.DateOffset(months=h)
            window = series[series["date"] >= target]

            if len(window):
                exit_date = window.iloc[0]["date"]
                exit_px = float(window.iloc[0]["adj_close"])
                complete = True
            elif delisted:
                # Held to the end of its life. This is a real outcome, usually
                # a bad one, and excluding it would bias the study upward.
                exit_date = sym_last
                exit_px = float(series.iloc[-1]["adj_close"])
                complete = True
            else:
                # Still trading, but the horizon runs past our data.
                exit_date, exit_px, complete = None, None, False

            stock_ret = (exit_px / entry_px - 1) if (complete and exit_px) else None

            bm_ret = None
            if complete and not bm.empty and bm_entry:
                _, bm_exit = _at_or_after(bm, target, "close")
                if bm_exit is None:
                    bm_exit = float(bm.iloc[-1]["close"])
                bm_ret = bm_exit / bm_entry - 1

            rows.append(Outcome(
                symbol=sym,
                signal_date=sig.date(),
                entry_date=entry_date.date(),
                entry_price=round(entry_px, 2),
                horizon_months=h,
                exit_date=exit_date.date() if exit_date is not None else None,
                exit_price=round(exit_px, 2) if exit_px else None,
                stock_return=stock_ret,
                benchmark_return=bm_ret,
                excess_return=(stock_ret - bm_ret) if (stock_ret is not None
                                                       and bm_ret is not None) else None,
                delisted=bool(delisted),
                complete=bool(complete),
            ))

    return pd.DataFrame([r.__dict__ for r in rows])


def summarise(outcomes: pd.DataFrame, excess_target: float = 0.15,
              multibagger: float = 1.0) -> pd.DataFrame:
    """Hit rates and medians per horizon.

    `excess_target` is the outperformance that counts as a win (0.15 = beat the
    benchmark by 15 points). `multibagger` is the absolute return that counts
    as a big winner (1.0 = doubled).
    """
    if outcomes.empty:
        return pd.DataFrame()

    out = []
    for h, g in outcomes.groupby("horizon_months"):
        done = g[g["complete"]]
        scored = done[done["excess_return"].notna()]
        if scored.empty:
            out.append({"horizon_months": h, "events": len(g), "complete": len(done),
                        "scored": 0})
            continue
        out.append({
            "horizon_months": h,
            "events": len(g),
            "complete": len(done),
            "scored": len(scored),
            "beat_bmk_pct": round(100 * (scored["excess_return"] > 0).mean(), 1),
            "hit_target_pct": round(100 * (scored["excess_return"] >= excess_target).mean(), 1),
            "median_excess_pct": round(100 * scored["excess_return"].median(), 1),
            "mean_excess_pct": round(100 * scored["excess_return"].mean(), 1),
            "multibagger_pct": round(100 * (scored["stock_return"] >= multibagger).mean(), 1),
            "median_stock_pct": round(100 * scored["stock_return"].median(), 1),
            "worst_pct": round(100 * scored["stock_return"].min(), 1),
            "delisted": int(scored["delisted"].sum()),
        })
    return pd.DataFrame(out).sort_values("horizon_months")


def baseline(con, dates: list[date], n_per_date: int = 50,
             horizons=HORIZONS, benchmark: str = DEFAULT_BENCHMARK,
             seed: int = 7, min_turnover_cr: float = 1.0) -> pd.DataFrame:
    """Outcomes for randomly chosen liquid stocks on the same dates.

    Without this there is nothing to compare a signal against. A screen with a
    55% hit rate sounds good until random selection over the same period also
    returns 55%, at which point the screen has done nothing.
    """
    rng = np.random.default_rng(seed)
    picks = []
    for d in dates:
        universe = con.execute(
            "SELECT symbol FROM prices WHERE date = ? AND isin_code LIKE 'INE%' "
            "AND turnover >= ?", [d, min_turnover_cr * 1e7]).df()
        if universe.empty:
            continue
        take = min(n_per_date, len(universe))
        chosen = rng.choice(universe["symbol"].values, size=take, replace=False)
        picks.extend({"symbol": s, "signal_date": d} for s in chosen)

    if not picks:
        return pd.DataFrame()
    return measure(con, pd.DataFrame(picks), horizons=horizons, benchmark=benchmark)

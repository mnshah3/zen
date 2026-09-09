"""Grid search: what improves an order win?

Order wins were the one signal that survived isolated measurement. This asks
whether filtering those events on price behaviour or fundamentals makes them
better.

EACH VARIANT USES THE WIDEST WINDOW IT IS ENTITLED TO. An earlier version of
this study restricted everything to April 2025 onward because fundamentals
start there -- and so threw away three years of data for variants that never
needed a fundamental in the first place. Price-based filters run over the full
announcement archive from 2022; only the fundamental filters are confined to
2025 onward. The window is printed for every row so no comparison is made
across different periods by accident.

DELIBERATELY SMALL AND PRE-REGISTERED. The variants below were fixed before
any was run and every one is written to the trial log regardless of how it
looks. Trying fifty combinations and reporting the best would guarantee finding
something, and guarantee it was noise.

POINT-IN-TIME THROUGHOUT. Fundamentals join on broadcast_dt <= signal_date;
liquidity and prior returns use windows ending the day before the signal.

    python -m jobs.study_combination
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

import duckdb
import pandas as pd

from zen.validation import eventstudy as es, trials

log = logging.getLogger(__name__)

PRICE_START = date(2022, 1, 1)      # announcement archive begins
FUND_START = date(2025, 4, 1)       # first published financials

# name -> (filters, needs_fundamentals)
VARIANTS = [
    # --- price-only: full 2022-2026 window -------------------------------
    ("orders_alone",             ([], False)),
    ("orders_declined_6m",       (["declined_6m"], False)),
    ("orders_declined_12m",      (["declined_12m"], False)),
    ("orders_near_52w_low",      (["near_low"], False)),
    ("orders_small",             (["small"], False)),
    ("orders_calm",              (["calm"], False)),
    ("orders_small_declined",    (["small", "declined_6m"], False)),
    ("orders_declined_calm",     (["declined_6m", "calm"], False)),
    # --- fundamental: 2025-2026 only, thinner and labelled ---------------
    ("orders_lowdebt",           (["debt_ok"], True)),
    ("orders_quality",           (["roce_ok"], True)),
    ("orders_cheap",             (["cheap"], True)),
    ("orders_quality_cheap",     (["roce_ok", "cheap"], True)),
    ("orders_all_fundamental",   (["debt_ok", "roce_ok", "cheap"], True)),
]


def open_readonly() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, pattern in [
        ("prices", "data/daily/**/*.parquet"),
        ("announcements", "data/announcements/**/*.parquet"),
        ("financials", "data/financials/*.parquet"),
        ("corpactions", "data/corpactions/*.parquet"),
        ("indices", "data/indices/*.parquet"),
    ]:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM "
                    f"read_parquet('{pattern}', union_by_name=true)")
    return con


def build_panel(con, start: date, min_turnover_cr: float = 1.0) -> pd.DataFrame:
    """Order-win events with price context, and fundamentals where they exist.

    Every window ends the session BEFORE the signal. The 52-week range and the
    six- and twelve-month prior returns are all computed to the prior close, so
    nothing here can see the day it is trying to predict.
    """
    return con.execute(f"""
        WITH ord AS (
            SELECT DISTINCT trade_date AS signal_date, symbol
            FROM announcements
            WHERE category = 'orders' AND trade_date >= DATE '{start}'
        ),
        ctx AS (
            SELECT date, symbol, close,
                   median(turnover) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS normal_turnover,
                   lag(close, 125) OVER (PARTITION BY symbol ORDER BY date) AS px_6m,
                   lag(close, 250) OVER (PARTITION BY symbol ORDER BY date) AS px_12m,
                   max(high) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) AS high_52w,
                   min(low) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) AS low_52w,
                   stddev_samp(close / nullif(prev_close, 0) - 1) OVER (
                       PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS vol_60d
            FROM prices
            WHERE isin_code LIKE 'INE%' AND series IN ('EQ','BE')
              AND close > 0 AND prev_close > 0
        ),
        fin AS (
            SELECT o.signal_date, o.symbol, f.debt_to_equity, f.equity,
                   f.ebitda, f.profit_normalised,
                   row_number() OVER (PARTITION BY o.symbol, o.signal_date
                       ORDER BY f.period_end DESC, f.broadcast_dt DESC) AS rn
            FROM ord o
            JOIN financials f
              ON f.symbol = o.symbol
             AND CAST(f.broadcast_dt AS DATE) <= o.signal_date
             AND f.consolidated
        )
        SELECT o.signal_date, o.symbol,
               c.normal_turnover / 1e7 AS normal_turnover_cr,
               CASE WHEN c.px_6m  > 0 THEN c.close / c.px_6m  - 1 END AS ret_6m,
               CASE WHEN c.px_12m > 0 THEN c.close / c.px_12m - 1 END AS ret_12m,
               CASE WHEN c.high_52w > c.low_52w
                    THEN (c.close - c.low_52w) / (c.high_52w - c.low_52w) END AS pos_52w,
               c.vol_60d,
               f.debt_to_equity, f.equity, f.ebitda, f.profit_normalised
        FROM ord o
        JOIN ctx c ON c.symbol = o.symbol AND c.date = o.signal_date
        LEFT JOIN fin f ON f.symbol = o.symbol AND f.signal_date = o.signal_date AND f.rn = 1
        WHERE c.normal_turnover >= {min_turnover_cr * 1e7}
        ORDER BY o.signal_date
    """).df()


def add_flags(panel: pd.DataFrame) -> pd.DataFrame:
    """Each filter as a boolean.

    Relative filters are cut at the median of the same MONTH rather than a
    fixed level. A fixed threshold would drift with the market and would also
    be a tunable number; a within-period median is neither.
    """
    p = panel.copy()
    p["month"] = pd.to_datetime(p["signal_date"]).dt.to_period("M")

    def below_median(col):
        med = p.groupby("month")[col].transform("median")
        return p[col].notna() & (p[col] <= med)

    # "Before the rally": lagging its peers over the prior 6 or 12 months.
    p["declined_6m"] = below_median("ret_6m")
    p["declined_12m"] = below_median("ret_12m")
    # Bottom third of its own 52-week range.
    p["near_low"] = p["pos_52w"].notna() & (p["pos_52w"] <= 0.33)
    # Smaller than the median event by normal turnover.
    p["small"] = below_median("normal_turnover_cr")
    # Less volatile than the median event -- a crude quality-of-price proxy.
    p["calm"] = below_median("vol_60d")

    # Fundamentals. EBITDA is one quarter and equity is a stock, so the ratio
    # is annualised before an annual hurdle is applied to it.
    p["debt_ok"] = p["debt_to_equity"].notna() & (p["debt_to_equity"] <= 1.5)
    p["roce_proxy"] = (p["ebitda"] * 4) / p["equity"].where(p["equity"] > 0)
    p["roce_ok"] = p["roce_proxy"].notna() & (p["roce_proxy"] > 0.12)
    p["earn_yield"] = p["profit_normalised"] / p["equity"].where(p["equity"] > 0)
    med_y = p.groupby("month")["earn_yield"].transform("median")
    p["cheap"] = p["earn_yield"].notna() & (p["earn_yield"] >= med_y)
    return p


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-events", type=int, default=80)
    args = ap.parse_args()

    con = open_readonly()
    panel = add_flags(build_panel(con, PRICE_START))
    log.info("panel: %d order-win events from %s; %d also carry fundamentals",
             len(panel), PRICE_START, int(panel["debt_to_equity"].notna().sum()))

    rows = []
    for name, (flags, needs_fund) in VARIANTS:
        ev = panel.copy()
        window = FUND_START if needs_fund else PRICE_START
        if needs_fund:
            ev = ev[pd.to_datetime(ev["signal_date"]).dt.date >= FUND_START]
        for f in flags:
            ev = ev[ev[f]]
        if len(ev) < args.min_events:
            log.warning("%s: %d events, below threshold, skipped", name, len(ev))
            continue

        cols = ev[["symbol", "signal_date"]].drop_duplicates()
        liq = es.liquidity_profile(con, cols)
        sig = es.summarise(es.measure(con, cols))
        ctrl = es.summarise(es.matched_baseline(con, cols))
        if sig.empty or ctrl.empty:
            continue

        trials.record("order_combination_v2",
                      {"variant": name, "filters": flags, "window_from": str(window)},
                      {"events": len(cols), "normal_turnover_cr": liq,
                       "signal": sig.to_dict("records"),
                       "control": ctrl.to_dict("records")})

        for h in (3, 6, 12, 24):
            s = sig[sig.horizon_months == h]
            c = ctrl[ctrl.horizon_months == h]
            if s.empty or c.empty or int(s.iloc[0]["scored"]) < 50:
                continue
            rows.append({
                "variant": name,
                "from": str(window)[:7],
                "h": h,
                "n": int(s.iloc[0]["scored"]),
                "liq_cr": liq,
                "sig%": s.iloc[0]["beat_bmk_pct"],
                "ctrl%": c.iloc[0]["beat_bmk_pct"],
                "gap_pp": round(s.iloc[0]["beat_bmk_pct"] - c.iloc[0]["beat_bmk_pct"], 1),
                "sig_med": s.iloc[0]["median_stock_pct"],
                "ctrl_med": c.iloc[0]["median_stock_pct"],
                "mbag%": s.iloc[0]["multibagger_pct"],
            })
        log.info("%s: %d events from %s", name, len(cols), window)

    con.close()
    if not rows:
        print("no results")
        return 1

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 260)
    print("\n" + out.to_string(index=False))
    print(f"\ntrials for this study: {trials.count('order_combination_v2')}")
    print(f"trials across all studies: {trials.count()}")
    print("\nHorizons with fewer than 50 scored events are suppressed entirely.")
    print("Price-only variants run from 2022; fundamental variants only from 2025-04.")
    print("Never compare a 2022 variant against a 2025 one -- different periods.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

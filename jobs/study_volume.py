"""Base-rate study: does unusual volume predict anything?

The first component tested in isolation, and the one that needs no filings --
eleven years of prices is enough on its own.

The question is deliberately narrow. Not "did the winners have volume spikes"
(they did, and so did hundreds of stocks that went nowhere) but "of every
stock that spiked on volume, what fraction beat the market". That inversion is
the whole point: P(outcome | signal), never P(signal | outcome).

Every variant tested is written to the trial log, including the ones that look
bad, because the count of attempts is what determines whether the best result
means anything.

    python -m jobs.study_volume --sample-dates 40
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

import duckdb
import pandas as pd

from zen.validation import eventstudy as es, trials

log = logging.getLogger(__name__)

# Deliberately few variants. Each one tested inflates the best result, and the
# point of this study is to measure honestly rather than to find a winner.
VARIANTS = [
    {"vol_x": 3,  "min_turnover_cr": 1.0, "require_up": False},
    {"vol_x": 5,  "min_turnover_cr": 1.0, "require_up": False},
    {"vol_x": 10, "min_turnover_cr": 1.0, "require_up": False},
    {"vol_x": 5,  "min_turnover_cr": 5.0, "require_up": False},
    {"vol_x": 5,  "min_turnover_cr": 1.0, "require_up": True},
]


def open_readonly() -> duckdb.DuckDBPyConnection:
    """Views over parquet, so this runs while a backfill holds the DB."""
    con = duckdb.connect()
    for name, pattern in [
        ("prices", "data/daily/**/*.parquet"),
        ("corpactions", "data/corpactions/*.parquet"),
        ("indices", "data/indices/*.parquet"),
    ]:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM "
                    f"read_parquet('{pattern}', union_by_name=true)")
    return con


def find_events(con, vol_x: float, min_turnover_cr: float,
                require_up: bool, lookback: int = 60,
                start: date | None = None) -> pd.DataFrame:
    """Sessions where a stock traded far above its OWN normal volume.

    Compared against each stock's own trailing median rather than a
    market-wide threshold, so a quiet small cap waking up ranks alongside a
    large cap. The median window ENDS on the prior session -- including the
    current day's volume in its own baseline would dilute the very spike being
    detected.
    """
    where_start = f"AND date >= DATE '{start}'" if start else ""
    up_filter = "AND close > prev_close" if require_up else ""
    return con.execute(f"""
        WITH base AS (
            SELECT date, symbol, close, prev_close, volume, turnover,
                   median(volume) OVER (
                       PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN {lookback} PRECEDING AND 1 PRECEDING
                   ) AS med_vol,
                   count(*) OVER (
                       PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN {lookback} PRECEDING AND 1 PRECEDING
                   ) AS hist
            FROM prices
            WHERE isin_code LIKE 'INE%' AND series IN ('EQ','BE')
              AND volume > 0 AND prev_close > 0 {where_start}
        )
        SELECT date AS signal_date, symbol,
               volume / med_vol AS vol_ratio,
               turnover / 1e7 AS turnover_cr,
               close / prev_close - 1 AS day_return
        FROM base
        WHERE hist >= {int(lookback * 0.6)}
          AND med_vol > 0
          AND volume / med_vol >= {vol_x}
          AND turnover >= {min_turnover_cr * 1e7}
          {up_filter}
        ORDER BY date
    """).df()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--max-events", type=int, default=4000,
                   help="cap per variant; sampled evenly across time, not truncated")
    p.add_argument("--baseline-dates", type=int, default=40)
    p.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = p.parse_args()

    con = open_readonly()
    results = []

    for variant in VARIANTS:
        ev = find_events(con, start=args.start, **variant)
        found = len(ev)
        if found == 0:
            log.warning("%s: no events", variant)
            continue

        # Sample evenly across time rather than taking the first N, which
        # would concentrate the study in whichever years were noisiest.
        if found > args.max_events:
            ev = ev.iloc[:: max(1, found // args.max_events)].head(args.max_events)
            log.info("%s: %d events, sampled to %d", variant, found, len(ev))
        else:
            log.info("%s: %d events", variant, found)

        outcomes = es.measure(con, ev[["symbol", "signal_date"]])
        summary = es.summarise(outcomes)
        if summary.empty:
            continue

        n = trials.record("volume_anomaly", variant,
                          {"events_found": found, "events_measured": len(ev),
                           "by_horizon": summary.to_dict("records")})
        summary.insert(0, "variant", str(variant))
        results.append(summary)
        log.info("trial %d recorded", n)

    # The comparison that makes the numbers mean anything.
    log.info("measuring random baseline")
    all_dates = con.execute(
        f"SELECT DISTINCT date FROM prices WHERE date >= DATE '{args.start}' "
        f"ORDER BY date").df()["date"].tolist()
    step = max(1, len(all_dates) // args.baseline_dates)
    base_out = es.baseline(con, all_dates[::step][:args.baseline_dates], n_per_date=60)
    base_sum = es.summarise(base_out)
    if not base_sum.empty:
        base_sum.insert(0, "variant", "RANDOM BASELINE")
        results.append(base_sum)
        trials.record("volume_anomaly", {"variant": "random_baseline"},
                      {"by_horizon": base_sum.to_dict("records")})

    con.close()

    if not results:
        print("no results")
        return 1

    out = pd.concat(results, ignore_index=True)
    pd.set_option("display.width", 250)
    print("\n" + out.to_string(index=False))
    print(f"\ntrials recorded for this study: {trials.count('volume_anomaly')}")
    print("Read every hit rate against the RANDOM BASELINE row, not against zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

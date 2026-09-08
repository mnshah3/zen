"""Base-rate study: do corporate filings predict returns?

Volume was tested first and failed. This tests the other half of the Sterlite
case study -- the filing itself. Capacity expansion, order wins, guidance,
credit rating actions, results, M&A, fund raising.

Same discipline as the volume study. The question is P(outcome | filing), never
P(filing | outcome), and every number is read against random selection on the
same dates rather than against zero.

Point-in-time is handled upstream: each filing carries trade_date, the first
session it could affect, which already rolls after-hours filings to the next
day. Entry is the open of the session after that.

    python -m jobs.study_filings
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

import duckdb
import pandas as pd

from zen.validation import eventstudy as es, trials

log = logging.getLogger(__name__)

# Categories worth testing as standalone signals. Governance, corporate actions
# and routine compliance are excluded: they are either noise or mechanical.
CATEGORIES = ["expansion", "orders", "guidance", "ratings", "results",
              "mna", "capital", "volume_query"]


def open_readonly() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, pattern in [
        ("prices", "data/daily/**/*.parquet"),
        ("announcements", "data/announcements/**/*.parquet"),
        ("corpactions", "data/corpactions/*.parquet"),
        ("indices", "data/indices/*.parquet"),
    ]:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM "
                    f"read_parquet('{pattern}', union_by_name=true)")
    return con


def find_events(con, category: str, min_turnover_cr: float = 1.0,
                start: date | None = None) -> pd.DataFrame:
    """Filings of one category, joined to a liquid, tradeable stock.

    The liquidity join uses the session the filing lands on, which is known at
    that point -- it does not look ahead. One event per symbol per day, since a
    company filing three announcements the same morning is one event, not three.
    """
    where_start = f"AND a.trade_date >= DATE '{start}'" if start else ""
    return con.execute(f"""
        SELECT DISTINCT a.trade_date AS signal_date, a.symbol
        FROM announcements a
        JOIN prices p
          ON p.symbol = a.symbol AND p.date = a.trade_date
        WHERE a.category = '{category}'
          AND p.isin_code LIKE 'INE%'
          AND p.turnover >= {min_turnover_cr * 1e7}
          {where_start}
        ORDER BY a.trade_date
    """).df()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--max-events", type=int, default=2500)
    p.add_argument("--baseline-dates", type=int, default=40)
    p.add_argument("--start", type=date.fromisoformat, default=date(2022, 1, 1))
    args = p.parse_args()

    con = open_readonly()

    span = con.execute(
        "SELECT min(trade_date), max(trade_date), count(*) FROM announcements"
    ).fetchone()
    log.info("announcement archive: %s to %s, %s filings", span[0], span[1], f"{span[2]:,}")

    results = []
    for cat in CATEGORIES:
        ev = find_events(con, cat, start=args.start)
        found = len(ev)
        if found < 100:
            log.warning("%s: only %d events, skipping", cat, found)
            continue

        if found > args.max_events:
            ev = ev.iloc[:: max(1, found // args.max_events)].head(args.max_events)
        log.info("%s: %d events (measuring %d)", cat, found, len(ev))

        outcomes = es.measure(con, ev)
        summary = es.summarise(outcomes)
        if summary.empty:
            continue

        trials.record("filing_category", {"category": cat},
                      {"events_found": found, "events_measured": len(ev),
                       "by_horizon": summary.to_dict("records")})
        summary.insert(0, "signal", cat)
        results.append(summary)

    # Random selection on comparable dates -- the only thing that makes the
    # hit rates above mean anything.
    log.info("measuring random baseline")
    dates = con.execute(
        f"SELECT DISTINCT date FROM prices WHERE date >= DATE '{args.start}' ORDER BY date"
    ).df()["date"].tolist()
    step = max(1, len(dates) // args.baseline_dates)
    base = es.summarise(es.baseline(con, dates[::step][:args.baseline_dates], n_per_date=60))
    if not base.empty:
        base.insert(0, "signal", "RANDOM BASELINE")
        results.append(base)
        trials.record("filing_category", {"category": "random_baseline"},
                      {"by_horizon": base.to_dict("records")})

    con.close()
    if not results:
        print("no results")
        return 1

    out = pd.concat(results, ignore_index=True)
    pd.set_option("display.width", 250)
    print("\n" + out.to_string(index=False))
    print(f"\ntrials recorded for this study: {trials.count('filing_category')}")
    print("Every row is read against RANDOM BASELINE, not against zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

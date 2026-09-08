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


def sample_evenly(ev, max_events: int):
    """Take an evenly spaced subset across the whole period.

    The previous form, ev.iloc[::step].head(n) with step = found // n, silently
    truncated to the EARLIEST events whenever found sat between one and two
    times the cap: step evaluates to 1, the slice returns everything, and head
    then cuts the tail off. That concentrates a study in whichever months came
    first rather than sampling the period, while the log still reports it as
    evenly sampled.
    """
    import numpy as np
    if len(ev) <= max_events:
        return ev
    idx = np.linspace(0, len(ev) - 1, max_events).round().astype(int)
    return ev.iloc[np.unique(idx)]


def find_events(con, category: str, min_turnover_cr: float = 1.0,
                start: date | None = None) -> pd.DataFrame:
    """Filings of one category, joined to a liquid, tradeable stock.

    The liquidity join uses the session the filing lands on, which is known at
    that point -- it does not look ahead. One event per symbol per day, since a
    company filing three announcements the same morning is one event, not three.
    """
    where_start = f"AND a.trade_date >= DATE '{start}'" if start else ""
    return con.execute(f"""
        WITH normal AS (
            SELECT date, symbol,
                   median(turnover) OVER (
                       PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
                   ) AS normal_turnover
            FROM prices
            WHERE isin_code LIKE 'INE%' AND series IN ('EQ','BE') AND turnover > 0
        )
        SELECT DISTINCT a.trade_date AS signal_date, a.symbol
        FROM announcements a
        JOIN normal n
          ON n.symbol = a.symbol AND n.date = a.trade_date
        WHERE a.category = '{category}'
          AND n.normal_turnover >= {min_turnover_cr * 1e7}
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

        ev = sample_evenly(ev, args.max_events)
        log.info("%s: %d events (measuring %d)", cat, found, len(ev))

        outcomes = es.measure(con, ev)
        summary = es.summarise(outcomes)
        if summary.empty:
            continue

        # Each signal gets its OWN control, matched on date and on the stock's
        # normal liquidity. A single shared baseline compared microcaps against
        # midcaps and produced a conclusion that did not survive audit.
        liq = es.liquidity_profile(con, ev)
        ctrl = es.summarise(es.matched_baseline(con, ev))

        trials.record("filing_category", {"category": cat},
                      {"events_found": found, "events_measured": len(ev),
                       "normal_turnover_cr": liq,
                       "by_horizon": summary.to_dict("records"),
                       "matched_control": ctrl.to_dict("records") if not ctrl.empty else None})
        summary.insert(0, "liq_cr", liq)
        summary.insert(0, "signal", cat)
        results.append(summary)
        if not ctrl.empty:
            # Same liquidity by construction -- that is the point of matching.
            ctrl.insert(0, "liq_cr", liq)
            ctrl.insert(0, "signal", "  ^ matched control")
            results.append(ctrl)

    # The old unmatched control, kept only as a reference row and clearly
    # labelled. It is 6x more liquid than a typical event stock, so it is not
    # a fair comparison -- it is here to show how misleading it was.
    log.info("measuring unmatched reference")
    dates = con.execute(
        f"SELECT DISTINCT date FROM prices WHERE date >= DATE '{args.start}' ORDER BY date"
    ).df()["date"].tolist()
    step = max(1, len(dates) // args.baseline_dates)
    base = es.summarise(es.baseline(con, dates[::step][:args.baseline_dates], n_per_date=60))
    if not base.empty:
        base.insert(0, "liq_cr", None)
        base.insert(0, "signal", "UNMATCHED ref (biased)")
        results.append(base)

    con.close()
    if not results:
        print("no results")
        return 1

    out = pd.concat(results, ignore_index=True)
    pd.set_option("display.width", 250)
    print("\n" + out.to_string(index=False))
    print(f"\ntrials recorded for this study: {trials.count('filing_category')}")
    print("Read each signal against the matched control DIRECTLY BELOW it.")
    print("liq_cr = median trailing turnover, Rs crore. Signal and control "
          "must be comparable or the result is a size effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

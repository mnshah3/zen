"""Daily job: fetch any bhavcopy days we are missing, store them, report.

Idempotent -- re-running never duplicates rows, so a failed day self-heals
on the next run.

    python -m jobs.update_prices              # last 7 days
    python -m jobs.update_prices --days 30
    python -m jobs.update_prices --start 2015-01-01 --end 2015-12-31
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from zen.data import bhavcopy, store

log = logging.getLogger(__name__)

# Days buffered before writing parquet. Small enough that an interrupted
# backfill loses little, large enough not to rewrite month files constantly.
FLUSH_EVERY = 120


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="lookback window")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))

    con = store.connect()
    have = store.stored_dates(con)

    session = bhavcopy._session()
    added_rows, added_days, frames, d = 0, [], [], start

    def flush() -> None:
        """Write buffered days out to parquet and clear the buffer."""
        nonlocal frames
        if not frames:
            return
        written = store.write_parquet(pd.concat(frames, ignore_index=True))
        log.info("flushed %d days to %d month files", len(frames), len(written))
        frames = []

    while d <= end:
        if d.weekday() < 5 and d not in have:
            # One malformed session must not abandon a multi-year backfill.
            # Skipped days are picked up by the next run, since the loop only
            # fetches dates missing from the archive.
            try:
                df = bhavcopy.fetch_day(d, raw_dir=Path("data/raw"), session=session)
            except Exception as e:
                log.warning("%s: skipped (%s: %s)", d, type(e).__name__, e)
                df = None
            if df is not None and not df.empty:
                n = store.upsert(con, df)
                frames.append(df)
                added_rows += n
                added_days.append(d)
                log.info("%s stored %d rows", d, n)
                # Periodic flush so an interrupted multi-year backfill keeps
                # everything it has already fetched.
                if len(frames) >= FLUSH_EVERY:
                    flush()
        d += timedelta(days=1)

    flush()

    cov = store.coverage(con)
    con.close()

    print(
        f"new days: {len(added_days)} ({added_rows:,} rows)\n"
        f"archive: {cov['first']} to {cov['last']} | "
        f"{cov['trading_days']:,} sessions | {cov['symbols']:,} symbols | "
        f"{cov['rows']:,} rows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

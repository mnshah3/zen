"""Fetch NSE index closes -- the benchmark series.

    python -m jobs.update_indices                 # catch up from the last stored day
    python -m jobs.update_indices --start 2015-01-01 --end 2026-09-18
    python -m jobs.update_indices --start 2018-01-01 --refetch   # re-read stored days

WHY THE ARCHIVE STOPPED AT 2026-09-07

Nothing ran this. The index archive was backfilled by hand once, and no
workflow ever called this job -- .github/workflows/daily_prices.yml fetches and
commits data/daily only -- so the benchmarks quietly froze while prices kept
updating. It is now scheduled by .github/workflows/daily_indices.yml, and two
changes stop the same gap from reopening unnoticed:

  * the default window starts the day after the last stored session (or
    --days back, whichever is earlier), so a run that fails for a fortnight is
    caught up by the next one that succeeds instead of leaving a permanent hole;
  * the job exits non-zero when the index archive trails the price archive by
    more than MAX_LAG_SESSIONS sessions, so a stale benchmark fails the
    workflow rather than sitting silently behind.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta

from zen.data import indices, store

log = logging.getLogger(__name__)
FLUSH_EVERY = 150

# NSE posts the index file in the evening; one session of lag is normal at the
# scheduled hour, two is tolerated, three means something is broken.
MAX_LAG_SESSIONS = 2


def lag_sessions(con) -> tuple[int, object, object]:
    """Price sessions stored after the last index session."""
    last_idx = con.execute("SELECT max(date) FROM indices").fetchone()[0]
    last_px = con.execute("SELECT max(date) FROM prices").fetchone()[0]
    if last_px is None:
        return 0, last_idx, last_px
    if last_idx is None:
        n = con.execute("SELECT count(DISTINCT date) FROM prices").fetchone()[0]
        return n, last_idx, last_px
    n = con.execute("SELECT count(DISTINCT date) FROM prices WHERE date > ?",
                    [last_idx]).fetchone()[0]
    return n, last_idx, last_px


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--refetch", action="store_true",
                   help="re-download days already stored and overwrite them")
    args = p.parse_args()

    con = store.connect()
    have = indices.stored_dates(con)

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))
    if args.start is None and have:
        start = min(start, max(have) + timedelta(days=1))

    session = indices._session()

    import pandas as pd
    frames, added, d = [], 0, start

    def flush():
        nonlocal frames
        if frames:
            indices.write_parquet(pd.concat(frames, ignore_index=True))
            frames = []

    log.info("indices: %s to %s%s", start, end, " (refetch)" if args.refetch else "")
    while d <= end:
        if d.weekday() < 5 and (args.refetch or d not in have):
            df = indices.fetch_day(d, session=session)
            if df is not None and not df.empty:
                added += indices.upsert(con, df, replace=args.refetch)
                frames.append(df)
                if len(frames) >= FLUSH_EVERY:
                    flush()
                    log.info("%s ... %d rows so far", d, added)
            time.sleep(indices.REQUEST_DELAY_S)
        d += timedelta(days=1)
    flush()

    n = con.execute("SELECT count(*) FROM indices").fetchone()[0]
    rng = con.execute("SELECT min(date), max(date), count(DISTINCT index_name) "
                      "FROM indices").fetchone()
    lag, last_idx, last_px = lag_sessions(con)
    con.close()
    print(f"added {added:,} rows | total {n:,} | {rng[0]} to {rng[1]} | "
          f"{rng[2]} indices")

    if lag > MAX_LAG_SESSIONS:
        log.error("index archive ends %s but prices run to %s (%d sessions "
                  "behind) -- the benchmarks are stale", last_idx, last_px, lag)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

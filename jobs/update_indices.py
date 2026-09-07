"""Fetch NSE index closes -- the benchmark series.

    python -m jobs.update_indices --days 7
    python -m jobs.update_indices --start 2015-01-01 --end 2026-09-07
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from zen.data import indices, store

log = logging.getLogger(__name__)
FLUSH_EVERY = 150


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    args = p.parse_args()

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))

    con = store.connect()
    have = indices.stored_dates(con)
    session = indices._session()

    import pandas as pd
    frames, added, d = [], 0, start

    def flush():
        nonlocal frames
        if frames:
            indices.write_parquet(pd.concat(frames, ignore_index=True))
            frames = []

    while d <= end:
        if d.weekday() < 5 and d not in have:
            df = indices.fetch_day(d, session=session)
            if df is not None and not df.empty:
                added += indices.upsert(con, df)
                frames.append(df)
                if len(frames) >= FLUSH_EVERY:
                    flush()
                    log.info("%s ... %d rows so far", d, added)
        d += timedelta(days=1)
    flush()

    n = con.execute("SELECT count(*) FROM indices").fetchone()[0]
    rng = con.execute("SELECT min(date), max(date), count(DISTINCT index_name) "
                      "FROM indices").fetchone()
    con.close()
    print(f"added {added:,} rows | total {n:,} | {rng[0]} to {rng[1]} | "
          f"{rng[2]} indices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

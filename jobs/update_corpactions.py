"""Fetch NSE corporate actions -- splits and bonuses need adjusting for.

    python -m jobs.update_corpactions --days 30
    python -m jobs.update_corpactions --start 2015-01-01 --end 2026-12-31
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from zen.data import corpactions, store


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    args = p.parse_args()

    end = args.end or (date.today() + timedelta(days=60))   # ex-dates are future-dated
    start = args.start or (end - timedelta(days=args.days))

    df = corpactions.fetch(start, end)
    con = store.connect()
    added = corpactions.upsert(con, df)
    corpactions.write_parquet(df)
    stats = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE action='split'), "
        "count(*) FILTER (WHERE action='bonus'), min(ex_date), max(ex_date) "
        "FROM corpactions").fetchone()
    con.close()
    print(f"added {added:,} | total {stats[0]:,} "
          f"({stats[1]:,} splits, {stats[2]:,} bonuses) | {stats[3]} to {stats[4]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

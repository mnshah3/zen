"""Fetch BSE's corporate announcements, with BSE's category and subcategory (zen.data.bse_announcements).

    python -m jobs.update_bse_announcements                    # the last 5 days, every day
    python -m jobs.update_bse_announcements --backfill --minutes 150
                                                               # days from 2018 not yet fetched,
                                                               # newest first, for 150 minutes
    python -m jobs.update_bse_announcements --start 2023-06-01 --end 2023-06-30

The scrip list is refreshed first, so every filing gets its ISIN. A day is recorded as fetched
only when its rows add up to BSE's count for it; anything else is left for the next run. Research
data: no strategy reads it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta

from zen.data import bse_announcements as bse


def _days(a) -> list[date]:
    today = date.today()
    if a.start:
        end = a.end or today
        return [a.start + timedelta(days=i) for i in range((end - a.start).days + 1)]
    if a.backfill:
        held = bse.done_days()
        last = today - timedelta(days=2)          # recent days belong to the daily run
        todo = [d for d in (bse.START + timedelta(days=i) for i in range((last - bse.START).days + 1))
                if d not in held]
        return todo[::-1]                          # newest first
    return [today - timedelta(days=i) for i in range(a.recent, -1, -1)]


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--minutes", type=float, default=150.0, help="stop starting new days after this long")
    p.add_argument("--recent", type=int, default=5, help="days back from today, without --backfill")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--pause", type=float, default=0.25)
    a = p.parse_args(argv)

    s = bse.session()
    try:
        scrips = bse.fetch_scrips(s)
        bse.save_scrips(scrips)
    except Exception as e:                         # the stored list is good enough for a day
        logging.warning("bse: scrip list not refreshed (%s)", e)
        scrips = bse.load_scrips()

    began, failures = time.monotonic(), 0
    fetched_days, rows_read, buffer, done = 0, 0, [], {}
    today = date.today()
    for d in _days(a):
        if time.monotonic() - began > a.minutes * 60:
            logging.info("bse: time budget used")
            break
        df = bse.fetch_day(d, s, pause=a.pause)
        if df is None:
            failures += 1
            if failures >= 8:
                logging.warning("bse: %d days failed in a row, stopping", failures)
                break
            continue
        failures = 0
        buffer.append(bse.with_isin(df, scrips))
        if d < today - timedelta(days=1):         # today and yesterday can still grow
            done[d] = len(df)
        fetched_days += 1
        rows_read += len(df)
        if len(buffer) >= 30:
            _flush(buffer, done)
            buffer, done = [], {}
        time.sleep(a.pause)
    _flush(buffer, done)
    print(f"bse: {fetched_days:,} days fetched, {rows_read:,} read ({len(bse.done_days()):,} days held)")
    return 0 if failures < 8 or fetched_days else 1


def _flush(buffer, done) -> None:
    import pandas as pd
    if buffer:
        frames = [x for x in buffer if len(x)]
        if frames:
            bse.write(pd.concat(frames, ignore_index=True))
    if done:
        bse.mark_done(done)


if __name__ == "__main__":
    sys.exit(main())

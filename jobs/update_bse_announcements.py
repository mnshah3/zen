"""Fetch BSE's corporate announcements, with BSE's category and subcategory (zen.data.bse_announcements).

    python -m jobs.update_bse_announcements                    # the last 5 days, every day
    python -m jobs.update_bse_announcements --backfill --minutes 150
                                                               # days from 2018 not yet fetched,
                                                               # newest first, for 150 minutes
    python -m jobs.update_bse_announcements --start 2023-06-01 --end 2023-06-30
    python -m jobs.update_bse_announcements --material --minutes 120
                                                               # the material labels a month at a time
                                                               # (bse.MATERIAL_LABELS), the gap first

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
    p.add_argument("--material", action="store_true", help="material labels by month, newest month first")
    p.add_argument("--workers", type=int, default=3, help="label-months fetched at once, with --material")
    a = p.parse_args(argv)
    if a.material:
        return material(a)

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


def material(a) -> int:
    """Each material label over each calendar month, the gap labels from 2018 to yesterday and the
    others (M&A, capital, ratings, which NSE labels from 2022) from 2018 to 2021. BSE caps a range
    at one calendar month and takes two to three seconds to answer, so `--workers` label-months are
    fetched at once, each thread with its own session. A label-month is recorded only when its rows
    match BSE's count; the current month is fetched to yesterday and left unrecorded."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import pandas as pd
    scrips = bse.load_scrips()
    held = bse.done_ranges()
    today = date.today()
    # Only where NSE has no label: its order, capacity and regulatory labels start on 23 Sep 2024
    # (and its archive in 2022), its M&A, capital and rating labels in 2022. Most valuable first:
    #   1. orders, expansion, contraction, licenses, Jan 2023 to Sep 2024 (NSE has none at all)
    #   2. regulatory labels, the same months
    #   3. every gap label, 2018 to 2022
    #   4. M&A, capital and ratings, 2018 to 2021
    def span(a, b):
        return list(pd.period_range(a, b, freq="M")[::-1])
    core = [x for x in bse.GAP_LABELS if bse.categorise(*x) != "regulatory"]
    regulatory = [x for x in bse.GAP_LABELS if bse.categorise(*x) == "regulatory"]
    plan = [(span("2023-01", "2024-09"), core), (span("2023-01", "2024-09"), regulatory),
            (span(bse.START, "2022-12"), list(bse.GAP_LABELS)), (span(bse.START, "2021-12"), list(bse.OTHER_MATERIAL_LABELS))]
    tasks = [(m, cat, sub) for months, labels in plan for m in months for cat, sub in labels
             if (str(m), sub) not in held]
    local = threading.local()

    def one(task):
        m, cat, sub = task
        if not hasattr(local, "s"):
            local.s = bse.session()
        first, last = m.start_time.date(), min(m.end_time.date(), today - timedelta(days=1))
        if last < first:
            return task, pd.DataFrame(columns=bse.COLUMNS)
        df = bse.fetch_label(cat, sub, first, last, local.s, pause=a.pause)
        time.sleep(a.pause)
        return task, df

    began, failures, fetched, rows_read = time.monotonic(), 0, 0, 0
    print(f"bse: {len(tasks):,} label-months to fetch", flush=True)
    chunk = max(30, a.workers * 20)
    with ThreadPoolExecutor(a.workers) as ex:
        for at in range(0, len(tasks), chunk):
            if time.monotonic() - began > a.minutes * 60:
                print(f"bse: {fetched:,} label-months fetched, {rows_read:,} read (time budget used)")
                return 0
            buffer, done, bad = [], [], 0
            for (m, cat, sub), df in ex.map(one, tasks[at:at + chunk]):
                if df is None:
                    bad += 1
                    continue
                fetched += 1
                rows_read += len(df)
                if len(df):
                    buffer.append(bse.with_isin(df, scrips))
                if m.end_time.date() < today:
                    done.append((str(m), cat, sub, len(df)))
            _flush(buffer, {})
            bse.mark_ranges(done)
            failures = failures + bad if bad else 0
            print(f"bse: {min(at + chunk, len(tasks)):,} of {len(tasks):,} done, {rows_read:,} filings, "
                  f"{bad} refused in this batch", flush=True)
            if failures >= 30:
                print(f"bse: {fetched:,} label-months fetched, {rows_read:,} read (stopped after failures)")
                return 1
    print(f"bse: {fetched:,} label-months fetched, {rows_read:,} read (complete)")
    return 0


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

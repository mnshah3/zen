"""Backfill NSE corporate announcements.

    python -m jobs.update_announcements --days 7
    python -m jobs.update_announcements --start 2022-01-01 --end 2026-09-08

Fetched a day at a time against a rate-limited exchange API, so this is
deliberately sequential. Parallelising it across processes gets the IP
blocked, which costs far more than the time it saves.

Days already stored are skipped, except the last --refresh-days up to --end,
which are always fetched again: the evening run at 20:15 IST stores a day
before its last filings are made, and skipping that day later would lose
them for good. Duplicates are ignored on the table's key.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

import pandas as pd

from zen.data import announcements as ann, freshness, store
from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)
FLUSH_EVERY = 60          # days buffered before writing parquet


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--parquet-only", action="store_true",
                   help="write parquet without touching DuckDB, so this can run "
                        "alongside another backfill (DuckDB allows one writer)")
    p.add_argument("--refresh-days", type=int, default=3,
                   help="always refetch this many days up to --end, stored or not")
    args = p.parse_args()

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))

    # DuckDB permits a single writer. When another backfill holds the lock this
    # job writes parquet only; the rows are merged into the database later by
    # jobs.rebuild_db, which reads the same parquet.
    con = None
    have: set = set()
    if not args.parquet_only:
        con = store.connect()
        ann.ensure_schema(con)
        have = {r[0] for r in con.execute(
            "SELECT DISTINCT CAST(an_dt AS DATE) FROM announcements").fetchall()}
        log.info("%d days already stored", len(have))
    else:
        # Recover what is already on disk so a restart does not refetch it.
        import glob
        import duckdb
        files = glob.glob("data/announcements/**/*.parquet", recursive=True)
        if files:
            mem = duckdb.connect()
            have = {r[0] for r in mem.execute(
                "SELECT DISTINCT CAST(an_dt AS DATE) FROM "
                "read_parquet('data/announcements/**/*.parquet', union_by_name=true)"
            ).fetchall()}
            mem.close()
        log.info("parquet-only mode; %d days already on disk", len(have))

    refresh_from = end - timedelta(days=max(args.refresh_days - 1, 0))
    session = _session()
    buf, added, days, d = [], 0, 0, start
    failed = 0

    def flush():
        nonlocal buf
        if buf:
            # With the database open, its copy of each row (carrying the
            # session_date fill_session_dates() set) is written to parquet too.
            ann.write_parquet(pd.concat(buf, ignore_index=True), con=con)
            buf = []

    while d <= end:
        # Weekends carry occasional filings, so they are not skipped here the
        # way they are for price data.
        if d not in have or d >= refresh_from:
            try:
                df = ann.fetch(d, d, session=session, strict=True)
            except Exception as e:
                log.warning("%s failed (%s)", d, e)
                failed += 1
                d += timedelta(days=1)
                continue
            if not df.empty:
                added += ann.upsert(con, df) if con else len(df)
                buf.append(df)
                days += 1
                if days % FLUSH_EVERY == 0:
                    flush()
                    log.info("%s ... %d rows stored", d, added)
        d += timedelta(days=1)
    flush()

    if con:
        stats = con.execute(
            "SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date) "
            "FROM announcements").fetchone()
        con.close()
        print(f"added {added:,} | total {stats[0]:,} filings, {stats[1]:,} companies | "
              f"{stats[2]} to {stats[3]}")
        if days and not failed:
            freshness.record("announcements")
    else:
        import duckdb
        mem = duckdb.connect()
        stats = mem.execute(
            "SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date) "
            "FROM read_parquet('data/announcements/**/*.parquet', union_by_name=true)"
        ).fetchone()
        mem.close()
        print(f"fetched {added:,} | parquet holds {stats[0]:,} filings, "
              f"{stats[1]:,} companies | {stats[2]} to {stats[3]}")
    if failed:
        print(f"{failed} day(s) failed to fetch; a later run retries them only while "
              f"they are inside its --refresh-days window, or if they were never stored")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

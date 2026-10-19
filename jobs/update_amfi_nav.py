"""Mutual fund NAVs from AMFI into data/mf_nav (zen/data/amfi_nav.py says what is kept and why).

    python -m jobs.update_amfi_nav                      # the sessions since the last one stored
    python -m jobs.update_amfi_nav --monthly 2018-06    # backfill: the last session of each month

The backfill reads one session a month (about 1.2 MB each), enough to compare returns over months
and years; from the first daily run on, every session is kept.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta

import duckdb
import pandas as pd
import requests

from zen.data import amfi_nav

log = logging.getLogger("update_amfi_nav")


def month_end_sessions(start: str, until: date) -> list[date]:
    """The last NSE session of each month from `start` (YYYY-MM) to the month before `until`."""
    con = duckdb.connect("data/zen.duckdb", read_only=True)
    days = pd.to_datetime(pd.Series([r[0] for r in con.execute("SELECT DISTINCT date FROM prices WHERE date >= ?",
                                                                 [pd.Timestamp(start + "-01").date()]).fetchall()]))
    con.close()
    last = days.groupby(days.dt.to_period("M")).max()
    return [d.date() for p, d in last.items() if p < pd.Period(until, "M")]


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--monthly", metavar="YYYY-MM", help="backfill the last session of each month from this month")
    a = ap.parse_args(argv)
    s = requests.Session()
    have = amfi_nav.stored_dates()
    if a.monthly:
        for d in month_end_sessions(a.monthly, date.today()):
            if d in have:
                continue
            for back in range(4):                      # a session AMFI published nothing for: the day before
                df = amfi_nav.fetch(s, d - timedelta(days=back), d - timedelta(days=back))
                if len(df):
                    break
                time.sleep(1)
            log.info("%s: %s rows%s", d, amfi_nav.store(df), "" if len(df) and df["date"].iloc[0] == d else f" (from {df['date'].iloc[0] if len(df) else 'nothing'})")
            time.sleep(1)
        return 0
    start = (max(have) + timedelta(days=1)) if have else date.today() - timedelta(days=7)
    end = date.today()
    if start > end:
        log.info("nothing new")
        return 0
    df = amfi_nav.fetch(s, start, end)
    log.info("%s to %s: %s rows over %s sessions", start, end, amfi_nav.store(df), df["date"].nunique() if len(df) else 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

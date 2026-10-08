"""Fetch NSE shareholding patterns and promoter pledges (zen.data.shareholding).

    python -m jobs.update_shareholding --days 10
    python -m jobs.update_shareholding --start 2023-01-01 --end 2026-10-08     # backfill

Shareholding filings are fetched by broadcast date; the pledge summary is a snapshot of every
company's latest disclosure, kept whenever a disclosure is new. Both are optional data: a
failure here is reported and exits non-zero, and nothing else in zen depends on them.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from zen.data import freshness, names as names_mod, shareholding, store
from zen.data.bhavcopy import _session


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--no-pledges", action="store_true")
    a = p.parse_args(argv)

    end = a.end or date.today()
    start = a.start or (end - timedelta(days=a.days))
    s = _session()
    sh = shareholding.fetch_shareholding(start, end, session=s)

    con = store.connect()
    try:
        added = shareholding.upsert_shareholding(con, sh)
        shareholding.write_shareholding(sh)
        pl_added, pl_n = 0, 0
        if not a.no_pledges:
            try:
                pl = shareholding.attach_symbols(shareholding.fetch_pledges(session=s), names_mod.load())
                pl_n = len(pl)
                new = shareholding.changed_pledges(con, pl)
                pl_added = shareholding.upsert_pledges(con, new)
                shareholding.write_pledges(new)
            except Exception as e:                                   # noqa: BLE001
                logging.warning("pledges unavailable (%s)", e)
        tot = con.execute("SELECT count(*), count(DISTINCT symbol), min(period_end), max(period_end) "
                          "FROM shareholding").fetchone()
    finally:
        con.close()
    print(f"shareholding: {len(sh):,} filings fetched, {added:,} new | total {tot[0]:,} filings, "
          f"{tot[1]:,} symbols, {tot[2]} to {tot[3]} | pledges: {pl_n:,} in the snapshot, {pl_added:,} new")
    if sh.empty and (end - start).days >= 7:
        # A week with no shareholding filing at all means the fetch failed.
        return 1
    freshness.record("shareholding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

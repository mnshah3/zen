"""Keep NSE's issued share counts current (zen/data/issued.py).

    python -m jobs.fetch_issued                 # the 400 symbols held longest, or never fetched
    python -m jobs.fetch_issued --limit 3000    # all of them, about 50 minutes at NSE's pace

Run daily, this goes round every traded symbol in about a week.
"""

from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from jobs.fetch_industry import recent_eq_symbols
from zen.data import industry, issued


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=400)
    a = p.parse_args(argv)
    eq, lo, hi = recent_eq_symbols()
    df = issued.load()
    when = dict(zip(df["symbol"], pd.to_datetime(df["fetched_at"]))) if len(df) else {}
    never = pd.Timestamp("2000-01-01")
    todo = sorted(eq, key=lambda s: (when.get(s, never), s))[: a.limit]
    print(f"symbols traded {lo} to {hi}: {len(eq):,} | fetching {len(todo):,}")
    try:
        issued.fetch(todo)
    except industry.Blocked as e:
        print(f"stopped: {e}. Progress is saved; the next run resumes.")
        return 2
    df = issued.load()
    print(f"counts held: {(df['note'] == 'ok').sum():,} of {len(df):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Give newly listed (and newly traded) symbols NSE's industry labels.

    python -m jobs.update_industry_new              # at most 60 symbols this run
    python -m jobs.update_industry_new --limit 200

data/reference/industry_nse.parquet was fetched once (jobs/fetch_industry.py) and a symbol
listed after that has no sector on the research pages. This job takes every EQ symbol traded
in the last 30 sessions of data/daily, keeps the ones the file has no final answer for, and
fetches those through the same resumable code (zen.data.industry.fetch_all). Symbols already
labelled are never fetched again.

The labels are research data: zen v1 does not read them. v2's sector cap does, but v2 is a
finished, published backtest and a symbol listed now was in none of its universes.
"""

from __future__ import annotations

import argparse
import logging
import sys

from jobs.fetch_industry import recent_eq_symbols
from zen.data import industry


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=60)
    a = p.parse_args(argv)

    eq, lo, hi = recent_eq_symbols()
    df = industry.load()
    have = set(df.loc[df["note"].map(industry.is_final), "symbol"]) if len(df) else set()
    missing = sorted(eq - have)
    todo = missing[: a.limit]
    print(f"EQ symbols traded {lo} to {hi}: {len(eq):,} | without a label: {len(missing):,} | fetching {len(todo):,}")
    if todo:
        try:
            industry.fetch_all(todo)
        except industry.Blocked as e:
            print(f"stopped: {e}. Progress is saved; the next run resumes.")
            return 2
    df = industry.load()
    labelled = df[df["note"] == "ok"]
    print(f"labels now: {len(labelled):,} symbols | still without one among recent EQ symbols: "
          f"{len(eq - set(df.loc[df['note'].map(industry.is_final), 'symbol'])):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

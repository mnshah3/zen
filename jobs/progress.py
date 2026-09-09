"""Live status of the legacy financials backfill.

Reads what is already on disk rather than talking to the running job, so it is
safe to run as often as you like and cannot disturb the download.

    python -m jobs.progress
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA = Path("data/financials")
LOG = Path("legacy_backfill.err")


def main() -> int:
    idx_path = DATA / "legacy_index.parquet"
    if not idx_path.exists():
        print("no index yet -- has the backfill been started?")
        return 1

    idx = pd.read_parquet(idx_path)
    expected = int(idx["has_xbrl"].sum())

    files = sorted(DATA.glob("legacy_2*.parquet"))
    done = 0
    frames = []
    for f in files:
        d = pd.read_parquet(f, columns=["symbol", "period_end", "revenue"])
        done += len(d)
        frames.append(d)

    # Rate comes from the log's own timestamps, which is the only honest
    # measure -- wall clock since launch would include the indexing phase.
    rate = None
    if LOG.exists():
        stamps = re.findall(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)",
                            LOG.read_text(errors="ignore"), re.M)
        if len(stamps) >= 2:
            t0 = datetime.strptime(stamps[0], "%Y-%m-%d %H:%M:%S")
            t1 = datetime.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S")
            secs = (t1 - t0).total_seconds()
            if secs > 0 and done:
                rate = done / secs

    pct = 100 * done / expected if expected else 0
    print(f"  documents   {done:,} of {expected:,}   ({pct:.1f}%)")
    print(f"  files       {len(files)} quarters written")
    if rate:
        left = (expected - done) / rate
        h, m = divmod(int(left // 60), 60)
        print(f"  rate        {rate:.2f} docs/sec")
        print(f"  eta         {h}h {m}m remaining")
    else:
        print("  rate        not enough data yet")

    if frames:
        all_rows = pd.concat(frames, ignore_index=True)
        all_rows["period_end"] = pd.to_datetime(all_rows["period_end"])
        print(f"  companies   {all_rows['symbol'].nunique():,} distinct so far")
        print(f"  periods     {all_rows['period_end'].min().date()} "
              f"to {all_rows['period_end'].max().date()}")
        got = all_rows["revenue"].notna().sum()
        print(f"  revenue     {got:,}/{len(all_rows):,} rows carry a revenue figure")

    if LOG.exists():
        tail = [l for l in LOG.read_text(errors="ignore").splitlines() if l.strip()][-2:]
        print("\n  " + "\n  ".join(tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())

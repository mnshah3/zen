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

    # Rate is measured between consecutive "N/M fetched" checkpoints in the
    # CURRENT log. Dividing all-time documents by the log's span looked simpler
    # and was wrong by a factor of twenty-five: the log is truncated on every
    # relaunch, so it charged eleven quarters of prior work against a two
    # minute window and reported ninety-four documents a second.
    rate = None
    if LOG.exists():
        marks = re.findall(
            r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO\s+(\d+)/\d+ fetched",
            LOG.read_text(errors="ignore"), re.M)
        if len(marks) >= 2:
            # Only spans inside one quarter are comparable; the counter resets
            # between them, so a decreasing count marks a boundary to skip.
            spans = []
            for (ta, na), (tb, nb) in zip(marks, marks[1:]):
                if int(nb) <= int(na):
                    continue
                secs = (datetime.strptime(tb, "%Y-%m-%d %H:%M:%S")
                        - datetime.strptime(ta, "%Y-%m-%d %H:%M:%S")).total_seconds()
                if secs > 0:
                    spans.append((int(nb) - int(na)) / secs)
            if spans:
                rate = sum(spans[-5:]) / len(spans[-5:])
        elif len(marks) == 1:
            # One checkpoint: measure it against the "fetching" line above it.
            m = re.search(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO fetching \d+",
                          LOG.read_text(errors="ignore"), re.M)
            if m:
                secs = (datetime.strptime(marks[0][0], "%Y-%m-%d %H:%M:%S")
                        - datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")).total_seconds()
                if secs > 0:
                    rate = int(marks[0][1]) / secs

    # A quarter's parquet is written only when the whole quarter finishes, so
    # counting files alone shows a frozen number for the hour it takes to work
    # through 2,500 documents -- indistinguishable from the job being wedged,
    # which is the one thing this script exists to rule out. The in-flight
    # count comes from the current quarter's last checkpoint.
    #
    # The checkpoint only counts as in-flight if it comes AFTER the last
    # "wrote legacy_*" line. A finished quarter leaves its final checkpoint as
    # the newest one in the log until the next quarter emits its first, and
    # counting it during that gap adds documents that are already inside the
    # written total -- which made the number jump to 34,293 and then fall back
    # to 31,593 on the next check.
    inflight = 0
    if LOG.exists():
        text = LOG.read_text(errors="ignore")
        last_write = max((m.end() for m in re.finditer(r"wrote legacy_\S+", text)),
                         default=-1)
        marks = [(m.start(), int(m.group(1)))
                 for m in re.finditer(r"INFO\s+(\d+)/\d+ fetched", text)]
        live = [n for pos, n in marks if pos > last_write]
        if live:
            inflight = live[-1]

    finished = LOG.exists() and "INFO done:" in LOG.read_text(errors="ignore")
    running = _is_running()

    pct = 100 * (done + inflight) / expected if expected else 0
    print(f"  documents   {done + inflight:,} of {expected:,}   ({pct:.1f}%)")
    if inflight:
        print(f"              {done:,} written + {inflight:,} in the quarter being fetched")
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

    # A finished run and a stuck one look identical otherwise: the counter
    # stops moving either way, and it stops BELOW 100% because some documents
    # are confirmed absent. That has to be stated, not left to be inferred from
    # a log line reading "done:".
    print()
    if finished and not running:
        print(f"  STATUS      COMPLETE. {done:,} parsed; {expected - done:,} confirmed "
              f"absent by NSE,")
        print("              so this never reaches 100%. Nothing left to fetch.")
    elif running:
        print("  STATUS      RUNNING")
    else:
        print("  STATUS      NOT RUNNING and not finished. Relaunch to resume "
              "-- completed")
        print("              quarters are skipped, so nothing is refetched.")
    return 0


def _is_running() -> bool:
    """Is a backfill process actually alive right now?"""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:                                            # noqa: BLE001
        return False
    return out.lower().count("python.exe") > 0


if __name__ == "__main__":
    sys.exit(main())

"""Fetch NSE's event calendar (board meetings for results, dividends and the rest) into
data/events/events.parquet, keeping when each event was first and last seen (zen.data.events).

    python -m jobs.update_events

Optional research data: nothing in zen's strategies reads it.
"""

from __future__ import annotations

import logging

from zen.data import events, store
from zen.data.bhavcopy import _session


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    new = events.fetch(_session())
    if new.empty:
        print("events: NSE's event calendar came back empty")
        return 1
    old = events.load()
    merged = events.merge(old, new)
    events.write(merged)
    con = store.connect()
    try:
        n = events.rebuild(con)
    finally:
        con.close()
    added = len(merged) - len(old)
    print(f"events: {len(new):,} listed now ({int(new['is_results'].sum()):,} for results), {added:,} new | "
          f"archive {n:,} events, {merged['event_date'].min()} to {merged['event_date'].max()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

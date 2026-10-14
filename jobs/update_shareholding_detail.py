"""Read who holds each company's public float from its shareholding filings
(zen.data.shareholding_detail).

    python -m jobs.update_shareholding_detail               # every filing not yet read
    python -m jobs.update_shareholding_detail --limit 400   # at most 400 this run, the newest
    python -m jobs.update_shareholding_detail --no-archive  # do not rebuild the archive's table
    python -m jobs.update_shareholding_detail --new-only    # skip filings read before a field was added

The filings are the ones data/shareholding already lists, read from those files, so this needs
no archive and runs on a fresh checkout (the backfill workflow uses --no-archive). Documents
are read in batches and each batch is written before the next. Optional research data:
nothing in zen's strategies reads it. The last line printed is the count still to read, for a
workflow that wants to run again until there are none.
"""

from __future__ import annotations

import argparse
import logging

from zen.data import shareholding_detail as detail

BATCH = 250


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int)
    p.add_argument("--pause", type=float, default=0.3, help="seconds between downloads")
    p.add_argument("--no-archive", action="store_true", help="leave the archive alone (a checkout without one)")
    p.add_argument("--new-only", action="store_true",
                   help="only filings never read, not ones read before a field was added (the daily job's way: "
                        "re-reading is for the backfill workflow, so the two never write the same files)")
    a = p.parse_args(argv)

    docs = detail.documents()
    waiting = docs[~docs["record_id"].astype(str).isin(detail.stored_ids(complete=not a.new_only))]
    # Newest first, always: old filings NSE no longer serves stay unread, and read first they
    # would end each run (fetch stops after a run of failures) before the new ones were reached.
    todo = waiting.iloc[::-1]
    if a.limit:
        todo = todo.head(a.limit)
    read = split = failed = 0
    for at in range(0, len(todo), BATCH):
        batch = todo.iloc[at:at + BATCH]
        rows, missed = detail.fetch(batch, pause=a.pause)
        detail.write(rows)
        read += len(rows)
        failed += missed
        split += int(rows["has_split"].sum()) if len(rows) else 0
        logging.info("shareholding detail: %d of %d documents done", min(at + BATCH, len(todo)), len(todo))
        if len(rows) + missed < len(batch):       # fetch gave up: NSE is refusing
            break
    n = None
    if not a.no_archive:
        from zen.data import store
        con = store.connect()
        try:
            n = detail.rebuild(con)
        finally:
            con.close()
    left = len(waiting) - read
    print(f"shareholding detail: {len(todo):,} documents to read, {read:,} read ({split:,} with the split), "
          f"{failed:,} not fetched" + (f" | table {n:,} rows" if n is not None else ""))
    print(f"remaining={left}")
    return 1 if len(todo) and read == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

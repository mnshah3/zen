"""Backfill quarterly financials from NSE's pre-2025 results endpoint.

This is the job that takes the fundamentals archive from eighteen months to
seven and a half years. See zen/data/financials_legacy.py for why the older
endpoint was previously thought empty -- it needs `period=Quarterly` and
returns nothing without it.

Long-running by nature: roughly nine thousand filings a year, each a separate
XBRL document. Written one parquet per quarter as it goes, so an interruption
costs the current quarter rather than the whole run.

    python -m jobs.backfill_legacy_financials
    python -m jobs.backfill_legacy_financials --start 2018-01-01 --end 2019-12-31
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from zen.data import financials_legacy as fl
from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, default=fl.XBRL_FROM)
    p.add_argument("--end", type=date.fromisoformat, default=date.today())
    p.add_argument("--out", type=Path, default=Path("data/financials"))
    p.add_argument("--sleep", type=float, default=0.12)
    p.add_argument("--index-only", action="store_true",
                   help="build and save the filing index without fetching documents")
    args = p.parse_args()

    s = _session()
    s.get(fl.WARMUP, timeout=30)

    log.info("indexing filings %s to %s", args.start, args.end)
    idx = fl.listing(s, args.start, args.end)
    if idx.empty:
        log.error("listing returned nothing -- has the endpoint changed?")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    idx.to_parquet(args.out / "legacy_index.parquet", index=False)
    log.info("indexed %d filings (%d with XBRL), %s to %s, %d companies",
             len(idx), int(idx["has_xbrl"].sum()),
             idx["period_end"].min(), idx["period_end"].max(),
             idx["symbol"].nunique())
    if args.index_only:
        return 0

    def rewarm(sess):
        """NSE cookies go stale; a fresh warm-up restores full speed."""
        try:
            sess.get(fl.WARMUP, timeout=30)
        except Exception as e:                                   # noqa: BLE001
            log.warning("re-warm failed: %s", e)

    # Quarter at a time so progress survives an interruption. Grouping on the
    # PERIOD rather than the broadcast date keeps each file self-describing.
    idx = idx[idx["has_xbrl"] & idx["period_end"].notna()].copy()
    idx["q"] = pd.to_datetime(idx["period_end"]).dt.to_period("Q")
    absent = fl.known_missing()
    if absent:
        log.info("%d documents already confirmed absent by NSE; not retrying", len(absent))

    total = 0
    for q, chunk in idx.groupby("q", sort=True):
        path = args.out / f"legacy_{q}.parquet"

        # A quarter is resumed rather than skipped. The earlier version treated
        # the file's existence as proof the quarter was complete, so any
        # document that failed mid-run became a permanent hole that no restart
        # could ever fill -- two quarters had lost over a hundred companies
        # between them before this was noticed.
        existing = pd.read_parquet(path) if path.exists() else None
        have = set(existing["xbrl_url"]) if existing is not None else set()
        todo = chunk[~chunk["xbrl_url"].isin(have | absent)]
        if todo.empty:
            log.info("%s complete (%d rows)", path.name, len(have))
            continue

        log.info("=== %s: %d to fetch (%d already held)", q, len(todo), len(have))
        df, unresolved = fl.fetch_documents(s, todo, sleep=args.sleep, rewarm=rewarm)
        if not unresolved.empty:
            fl.record_missing(unresolved)
            n_gone = int((unresolved["outcome"] == "missing").sum())
            log.info("  %d unresolved (%d confirmed absent, %d retryable)",
                     len(unresolved), n_gone, len(unresolved) - n_gone)
        if df.empty:
            log.warning("%s produced no parsable rows this pass", q)
            continue

        df["period_end"] = pd.to_datetime(df["period_end"])
        if existing is not None:
            existing["period_end"] = pd.to_datetime(existing["period_end"])
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["xbrl_url"])
        df.to_parquet(path, index=False)
        total += len(df) - len(have)
        log.info("wrote %s (%d rows total, +%d this pass, running total %d)",
                 path.name, len(df), len(df) - len(have), total)

    log.info("done: %d new rows written", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

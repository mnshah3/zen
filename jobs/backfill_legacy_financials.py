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

    # Quarter at a time so progress survives an interruption. Grouping on the
    # PERIOD rather than the broadcast date keeps each file self-describing.
    idx = idx[idx["has_xbrl"] & idx["period_end"].notna()].copy()
    idx["q"] = pd.to_datetime(idx["period_end"]).dt.to_period("Q")

    total = 0
    for q, chunk in idx.groupby("q", sort=True):
        path = args.out / f"legacy_{q}.parquet"
        if path.exists():
            log.info("%s already present, skipping %d filings", path.name, len(chunk))
            continue
        log.info("=== %s: %d filings", q, len(chunk))
        df = fl.fetch_documents(s, chunk, sleep=args.sleep)
        if df.empty:
            log.warning("%s produced no parsable rows", q)
            continue
        df["period_end"] = pd.to_datetime(df["period_end"])
        df.to_parquet(path, index=False)
        total += len(df)
        log.info("wrote %s (%d rows, running total %d)", path.name, len(df), total)

    log.info("done: %d rows written", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

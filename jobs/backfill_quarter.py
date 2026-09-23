"""Fill a quarter the regular financials fetch left short.

The March 2025 quarter held 1,516 companies against about 2,100 before it and
2,200 after. Nothing about March 2025 was unusual. The listing code stopped at
the first page that failed to load and returned what it had, and a short list
in results season looks exactly like a normal one. That silent stop is fixed in
zen/data/financials.py. This job refills the damage.

It lists every financial filing broadcast across a window wide enough to catch
late filers, keeps those for the target quarter, drops documents already held
(by XBRL URL, so revisions are kept), fetches the rest and appends them to that
quarter's parquet. It never rewrites or removes a row it did not fetch.

Most of what it finds in an otherwise complete quarter are REVISIONS: a later
version of a filing already held. They are kept alongside the original, and
the engine uses whichever had been broadcast by each decision date.

    python -m jobs.backfill_quarter --period 2025-03-31
    python -m jobs.backfill_quarter --period 2022-06-30 --dry-run

Quarters before 2025 come from the older endpoint and are merged into its
legacy_YYYYQn.parquet file. Its global filing index is left alone.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from zen.data import financials
from zen.data import financials_legacy as fl
from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)


def listing_window(start: date, end: date, step_days: int = 5) -> pd.DataFrame:
    """Walk the broadcast window in short steps.

    Short steps keep each listing to a few pages, which is what made the
    original fetch fragile. Any step that still fails raises, so a gap can
    never pass for an empty week again.
    """
    frames, d = [], start
    while d <= end:
        e = min(d + timedelta(days=step_days - 1), end)
        L = financials.listing(start=d, end=e)
        log.info("  %s..%s: %d filings", d, e, len(L))
        if not L.empty:
            frames.append(L)
        d = e + timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", type=date.fromisoformat, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    pe = pd.Timestamp(args.period)
    # Quarters before 2025 live in the older endpoint's files, one per quarter.
    legacy = pe.year < 2025
    path = (financials.PARQUET_DIR / f"legacy_{pe.to_period('Q')}.parquet" if legacy
            else financials.PARQUET_DIR / f"{pe:%Y-%m}.parquet")
    held = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=financials.COLUMNS)
    log.info("%s holds %d rows, %d companies", path.name, len(held), held["symbol"].nunique())

    # Quarter end to eighteen months after. The statutory deadline is 45 days,
    # 60 for the March quarter, but revisions and very late filers arrive far
    # later: in the March 2025 quarter 40 companies filed after 150 days, the
    # latest after 494. The engine only ever uses a version after its
    # broadcast date, so fetching late versions cannot leak anything.
    start = (pe + pd.Timedelta(days=1)).date()
    end = min((pe + pd.Timedelta(days=540)).date(), date.today())
    if legacy:
        sess = _session()
        sess.get(fl.WARMUP, timeout=30)
        L = fl.listing(sess, start, end)
        L = L[L["has_xbrl"]] if not L.empty else L
    else:
        L = listing_window(start, end)
    if L.empty:
        log.error("listing returned nothing for %s..%s", start, end)
        return 1
    L["period_end"] = pd.to_datetime(L["period_end"])
    target = L[L["period_end"] == pe].drop_duplicates("xbrl_url")
    todo = target[~target["xbrl_url"].isin(set(held["xbrl_url"]))].copy()
    gain = set(todo["symbol"]) - set(held["symbol"])
    log.info("listed %d filings for %s across %d companies; %d documents not held, "
             "%d companies entirely missing", len(target), pe.date(),
             target["symbol"].nunique(), len(todo), len(gain))
    if args.dry_run or todo.empty:
        return 0

    todo["has_xbrl"] = True
    s = _session()
    s.get(financials.WARMUP, timeout=30)
    rows, unresolved = fl.fetch_documents(s, todo, workers=5, max_rate=8.0)
    if not unresolved.empty:
        log.warning("%d documents unresolved: %s", len(unresolved),
                    unresolved["outcome"].value_counts().to_dict())
    if rows.empty:
        log.error("nothing parsed")
        return 1
    rows = rows.reindex(columns=financials.COLUMNS)
    rows["period_end"] = pd.to_datetime(rows["period_end"])
    held["period_end"] = pd.to_datetime(held["period_end"])
    # Only documents not already held are appended. Existing rows are never
    # de-duplicated or rewritten, even the handful that are stored twice.
    new = rows[~rows["xbrl_url"].isin(set(held["xbrl_url"]))].drop_duplicates("xbrl_url")
    out = pd.concat([held, new], ignore_index=True)
    out.to_parquet(path, index=False, compression="zstd")
    log.info("wrote %s: %d rows (+%d), %d companies (was %d)", path.name, len(out),
             len(out) - len(held), out["symbol"].nunique(), held["symbol"].nunique())
    return 0


if __name__ == "__main__":
    sys.exit(main())

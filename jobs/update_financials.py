"""Fetch quarterly financials from NSE integrated filings.

    python -m jobs.update_financials --days 30
    python -m jobs.update_financials --start 2025-01-01 --end 2026-09-30
    python -m jobs.update_financials --symbol RELIANCE
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

import pandas as pd

from zen.data import financials as fin, store, xbrl_cache

log = logging.getLogger(__name__)
CHUNK_DAYS = 30          # one results-season slice at a time
FLUSH_EVERY = 400


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--symbol")
    args = p.parse_args()

    con = store.connect()
    fin.ensure_schema(con)

    if args.symbol:
        df = fin.fetch(symbol=args.symbol)
        fin.write_parquet(df)
        added = fin.upsert(con, df)
        print(f"{args.symbol}: {len(df)} filings, {added} new")
        con.close()
        return 0

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))

    # Documents already stored are skipped, by XBRL URL. Skipping by (symbol,
    # quarter, basis) instead, as this used to, meant a company's revised
    # filing was never fetched, so the live archive and a backfilled one held
    # different versions of the same quarter. Revisions are kept: the engine
    # uses whichever version had been broadcast by each decision date.
    have = set()
    try:
        have = {r[0] for r in con.execute("SELECT xbrl_url FROM financials").fetchall()}
    except Exception:                                            # noqa: BLE001
        pass
    log.info("%d filing documents already stored", len(have))

    total_new, buf, cur = 0, [], start
    failed_windows, failed_docs = [], 0
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            idx = fin.listing(start=cur, end=stop)
        except Exception as e:                                   # noqa: BLE001
            # Keep going so one bad window does not cost the others, but
            # remember it: the run must end in failure, not quietly succeed
            # with a month missing.
            log.error("%s to %s: listing failed (%s)", cur, stop, e)
            failed_windows.append(f"{cur}..{stop}")
            cur = stop + timedelta(days=1)
            continue

        if not idx.empty:
            idx = idx[~idx["xbrl_url"].isin(have)]

        if not idx.empty:
            session = fin._session()
            rows = []
            for meta in idx.to_dict("records"):
                try:
                    # A stored copy is read instead of downloaded; a download
                    # is stored before it is parsed (zen/data/xbrl_cache.py).
                    content = xbrl_cache.get(meta["xbrl_url"])
                    if content is None:
                        resp = session.get(meta["xbrl_url"], timeout=60)
                        resp.raise_for_status()
                        content = resp.content
                        xbrl_cache.put(meta["xbrl_url"], content)
                    facts = fin.parse_xbrl(content)
                except Exception:                                # noqa: BLE001
                    failed_docs += 1        # not in `have`, so retried next run
                    continue
                if facts:
                    rows.append(fin._derive({**meta, **facts}))
                    have.add(meta["xbrl_url"])
            if rows:
                df = pd.DataFrame(rows).reindex(columns=fin.COLUMNS)
                # Parquet first. The database is rebuilt from parquet, so a row
                # that reached the database but not the file would be skipped
                # by every later run and then vanish at the next rebuild.
                buf.append(df)
                if sum(len(b) for b in buf) >= FLUSH_EVERY:
                    fin.write_parquet(pd.concat(buf, ignore_index=True))
                    buf = []
                total_new += fin.upsert(con, df)
                log.info("%s to %s: +%d (total %d)", cur, stop, len(rows), total_new)
        cur = stop + timedelta(days=1)

    if buf:
        fin.write_parquet(pd.concat(buf, ignore_index=True))

    stats = con.execute(
        "SELECT count(*), count(DISTINCT symbol), count(*) FILTER (WHERE has_balance_sheet), "
        "min(period_end), max(period_end) FROM financials").fetchone()
    con.close()
    print(f"added {total_new:,} | total {stats[0]:,} filings, {stats[1]:,} companies, "
          f"{stats[2]:,} with balance sheet | {stats[3]} to {stats[4]}")
    if failed_docs:
        print(f"{failed_docs} documents failed to download or parse; they will be retried")
    if failed_windows:
        print(f"LISTING FAILED for {len(failed_windows)} window(s): {', '.join(failed_windows)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

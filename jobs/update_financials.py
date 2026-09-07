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

from zen.data import financials as fin, store

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
        added = fin.upsert(con, df)
        fin.write_parquet(df)
        print(f"{args.symbol}: {len(df)} filings, {added} new")
        con.close()
        return 0

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))

    # Already-stored filings are skipped so a resumed backfill does not refetch.
    have = set()
    try:
        have = {(r[0], r[1], r[2]) for r in con.execute(
            "SELECT symbol, period_end, consolidated FROM financials").fetchall()}
    except Exception:
        pass
    log.info("%d filings already stored", len(have))

    total_new, buf, cur = 0, [], start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            idx = fin.listing(start=cur, end=stop)
        except Exception as e:
            log.warning("%s to %s: listing failed (%s)", cur, stop, e)
            cur = stop + timedelta(days=1)
            continue

        if not idx.empty:
            idx = idx[~idx.apply(
                lambda r: (r.symbol, r.period_end, r.consolidated) in have, axis=1)]

        if not idx.empty:
            session = fin._session()
            rows = []
            for meta in idx.to_dict("records"):
                try:
                    resp = session.get(meta["xbrl_url"], timeout=60)
                    resp.raise_for_status()
                    facts = fin.parse_xbrl(resp.content)
                except Exception:
                    continue
                if facts:
                    rows.append(fin._derive({**meta, **facts}))
                    have.add((meta["symbol"], meta["period_end"], meta["consolidated"]))
            if rows:
                df = pd.DataFrame(rows).reindex(columns=fin.COLUMNS)
                total_new += fin.upsert(con, df)
                buf.append(df)
                if sum(len(b) for b in buf) >= FLUSH_EVERY:
                    fin.write_parquet(pd.concat(buf, ignore_index=True))
                    buf = []
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

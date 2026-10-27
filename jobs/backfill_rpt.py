"""Related-party transactions for the filings already in the archive (zen.data.rpt), from the local
XBRL copy and, with --download, from NSE for the documents not held (stored in the copy first, at
--rate a second).

    python -m jobs.backfill_rpt --halves 2              # the last two half years, local copies only
    python -m jobs.backfill_rpt --halves 2 --download   # and fetch the rest

WHICH FILINGS. The standalone results for the quarters that end a half year (September and March),
where the integrated filing carries the disclosure; every revision of them. New filings get theirs
as they are fetched (jobs/update_financials.py).

Writes data/rpt/<quarter>.parquet and prints how many filings were read and how many disclose
transactions.
"""
from __future__ import annotations

import argparse
import logging
import time

import pandas as pd
import requests

from zen.data import financials as fin, rpt, xbrl_cache

log = logging.getLogger("backfill_rpt")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "*/*"}


def filings(halves: int) -> pd.DataFrame:
    files = [p for p in fin.statement_files() if not p.stem.startswith("legacy")]
    df = pd.concat([pd.read_parquet(p, columns=["symbol", "period_end", "broadcast_dt", "consolidated", "xbrl_url"]) for p in files],
                   ignore_index=True)
    df = df[df["xbrl_url"].notna() & ~df["consolidated"].astype(bool)].copy()
    df["period_end"] = pd.to_datetime(df["period_end"])
    ends = sorted(d for d in df["period_end"].dropna().unique() if pd.Timestamp(d).month in (3, 9))[-halves:]
    return df[df["period_end"].isin(ends)].sort_values("period_end", ascending=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--halves", type=int, default=2)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--rate", type=float, default=1.0, help="downloads a second")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    todo = filings(a.halves)
    done = set()
    for p in sorted(rpt.PARQUET_DIR.glob("*.parquet")):
        done |= set(pd.read_parquet(p, columns=["xbrl_url"])["xbrl_url"])
    s = requests.Session()
    s.headers.update(UA)
    out, n_read, n_rpt, missing, last = [], 0, 0, 0, 0.0
    for r in todo.to_dict("records"):
        if r["xbrl_url"] in done:
            continue
        b = xbrl_cache.get(r["xbrl_url"])
        if b is None and a.download:
            wait = 1.0 / a.rate - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            try:
                resp = s.get(r["xbrl_url"], timeout=20)
                if resp.ok:
                    xbrl_cache.put(r["xbrl_url"], resp.content)
                    b = xbrl_cache.get(r["xbrl_url"])
            except requests.RequestException:
                b = None
        if b is None:
            missing += 1
            continue
        n_read += 1
        rows = rpt.rows_for(r, rpt.parse_rpt(b))
        if rows:
            n_rpt += 1
            out += rows
        if (n_read + missing) % 250 == 0:
            log.info("read %d, disclosing %d, not held %d", n_read, n_rpt, missing)
        if len(out) >= 5000:
            rpt.write_parquet(pd.DataFrame(out))
            out = []
    if out:
        rpt.write_parquet(pd.DataFrame(out))
    print(f"filings read {n_read}, disclosing transactions {n_rpt}, not held locally {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

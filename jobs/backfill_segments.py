"""Segments for the filings already in the archive (zen.data.segments), from the local XBRL copy and,
with --download, from NSE for the documents not held (stored in the copy first, at --rate a second).

    python -m jobs.backfill_segments --quarters 8              # the last eight quarters, local copies only
    python -m jobs.backfill_segments --quarters 8 --download   # and fetch the rest
    python -m jobs.backfill_segments --quarters 28 --legacy --download   # back to 2019: the filings
                                       # before the 2025 format carry segments too (zen.data.segments)

WHICH FILINGS. Each company's filings for the quarters asked, the consolidated ones where the company
filed consolidated results for that quarter, else the standalone; every revision of them. New filings
get their segments as they are fetched (jobs/update_financials.py).

Writes data/segments/<quarter>.parquet and prints how many filings were read, how many report
segments and how many of those reconcile. A filing read that reports no segments (a company with one
business) goes into CHECKED, so a later run skips it too: the run is resumable, and on GitHub, where
no copy of the documents is kept, a second run fetches only what the first could not. --minutes stops
reading in time for the workflow to commit what was read.
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from zen.data import financials as fin, segments, xbrl_cache

log = logging.getLogger("backfill_segments")
CHECKED = Path(__file__).resolve().parents[1] / "data" / "segments_checked.txt"   # filings read, no segments reported
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "*/*"}


def filings(quarters: int, legacy: bool = False) -> pd.DataFrame:
    files = [p for p in fin.statement_files()
             if not p.stem.startswith("legacy") or (legacy and p.stem != "legacy_missing")]
    df = pd.concat([pd.read_parquet(p, columns=["symbol", "period_end", "broadcast_dt", "consolidated", "xbrl_url"]) for p in files],
                   ignore_index=True)
    df = df[df["xbrl_url"].notna()].copy()
    df["period_end"] = pd.to_datetime(df["period_end"])
    keep = sorted(df["period_end"].dropna().unique())[-quarters:]
    df = df[df["period_end"].isin(keep)]
    cons = df.groupby(["symbol", "period_end"])["consolidated"].transform("max")
    return df[df["consolidated"] == cons]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=8)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--legacy", action="store_true", help="also the quarters filed before the 2025 format")
    ap.add_argument("--rate", type=float, default=3.0, help="downloads a second")
    ap.add_argument("--minutes", type=float, default=None, help="stop reading after this long")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    todo = filings(a.quarters, a.legacy)
    done = set()
    for p in sorted(segments.PARQUET_DIR.glob("*.parquet")):
        done |= set(pd.read_parquet(p, columns=["xbrl_url"])["xbrl_url"])
    checked = set(CHECKED.read_text(encoding="utf-8").split()) if CHECKED.exists() else set()
    new_checked: list[str] = []

    def save():
        nonlocal out, new_checked
        if out:
            segments.write_parquet(pd.DataFrame(out))
            out = []
        if new_checked:
            with CHECKED.open("a", encoding="utf-8") as f:
                f.write("\n".join(new_checked) + "\n")
            new_checked = []
    s = requests.Session()
    s.headers.update(UA)
    out, n_read, n_seg, n_ok, missing, last = [], 0, 0, 0, 0, 0.0
    for r in todo.to_dict("records"):
        if r["xbrl_url"] in done or r["xbrl_url"] in checked:
            continue
        if a.minutes is not None and time.monotonic() - t0 > a.minutes * 60:
            log.info("time is up: stopping, the rest for the next run")
            break
        b = xbrl_cache.get(r["xbrl_url"])
        if b is None and a.download:
            wait = 1.0 / a.rate - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            try:
                resp = s.get(r["xbrl_url"], timeout=(6, 12))
                if resp.ok:
                    xbrl_cache.put(r["xbrl_url"], resp.content)
                    b = xbrl_cache.get(r["xbrl_url"])
            except requests.RequestException:
                b = None
        if b is None:
            missing += 1
            continue
        n_read += 1
        parsed = segments.parse_segments(b)
        rows = segments.rows_for(r, parsed)
        if rows:
            n_seg += 1
            n_ok += bool(parsed["reconciles"])
            out += rows
        else:
            new_checked.append(r["xbrl_url"])
        if (n_read + missing) % 250 == 0:
            log.info("read %d, with segments %d, reconciling %d, not held %d", n_read, n_seg, n_ok, missing)
        if len(out) >= 1000 or len(new_checked) >= 500:
            save()
    save()
    print(f"filings read {n_read}, with segments {n_seg}, reconciling {n_ok}, not held locally {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

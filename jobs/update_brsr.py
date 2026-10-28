"""Business Responsibility and Sustainability Reports filed on NSE in the last --days days, read
for what each company does (zen.data.brsr) and stored in data/brsr/brsr.parquet.

    python -m jobs.update_brsr --days 10                   # nightly: the new filings
    python -m jobs.update_brsr --days 420 --latest-only    # catch-up: each company's latest report
    python -m jobs.update_brsr --days 420 --latest-only --refill    # and again, those stored before
                                                                    # the group companies were read

A filing already stored is not read again. Documents are fetched at --rate a second.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta

import pandas as pd
import requests

from zen.data import brsr

log = logging.getLogger("update_brsr")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Referer": brsr.PAGE}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--latest-only", action="store_true", help="only each company's latest report")
    ap.add_argument("--rate", type=float, default=2.0, help="downloads a second")
    ap.add_argument("--refill", action="store_true", help="read again the stored filings without group companies")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    s = requests.Session()
    s.headers.update(UA)
    try:
        s.get(brsr.PAGE, timeout=25)
    except requests.RequestException:
        pass
    to = date.today()
    frm = to - timedelta(days=a.days)
    r = s.get(brsr.LIST_URL + f"&from_date={frm:%d-%m-%Y}&to_date={to:%d-%m-%Y}", timeout=90)
    r.raise_for_status()
    lst = brsr.listing(r.json())
    if a.latest_only and len(lst):
        lst = lst.sort_values(["fy_to", "filed"], ascending=False).drop_duplicates("symbol")
    stored = pd.read_parquet(brsr.PARQUET) if brsr.PARQUET.exists() else pd.DataFrame(columns=["xbrl_url"])
    done = set(stored["xbrl_url"])
    if a.refill:              # stored before group_cos was read (28 Oct 2026): read again
        done -= set(stored.loc[stored["group_cos"].isna(), "xbrl_url"]) if "group_cos" in stored else done
    todo = lst[~lst["xbrl_url"].isin(done)]
    log.info("%d filings listed, %d to read", len(lst), len(todo))
    rows, n_ok, n_bad, last = [], 0, 0, 0.0
    for m in todo.to_dict("records"):
        wait = 1.0 / a.rate - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            resp = s.get(m["xbrl_url"], timeout=(6, 30))
            resp.raise_for_status()
            parsed = brsr.parse_brsr(resp.content)
        except requests.RequestException:
            parsed = None
        if not parsed:
            n_bad += 1
            continue
        rows.append(brsr.row_for(m, parsed))
        n_ok += 1
        if len(rows) >= 100:
            brsr.write_parquet(rows)
            rows = []
            log.info("read %d, not read %d", n_ok, n_bad)
    brsr.write_parquet(rows)
    print(f"brsr: read {n_ok}, not read {n_bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

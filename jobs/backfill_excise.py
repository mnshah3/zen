"""Add each stored quarter's excise duty (financials.EXCISE_LINE, column excise_duty, 21 Oct 2026) to
the results of the companies that file one.

    python -m jobs.backfill_excise                 # find the companies, fetch, write data/financials
    python -m jobs.backfill_excise --dry-run       # the same, writing only the report

WHICH COMPANIES. Those whose filings in the local XBRL copy (zen.data.xbrl_cache) already show an
excise line: on 21 Oct 2026, 39 of the 2,669 companies with results, all in tobacco, liquor, fuel,
natural gas and sugar (ITC, Reliance, IOC, BPCL, HPCL, United Spirits, Radico ...). Excise on other
goods ended with GST in Jul 2017, before zen's results begin.

WHAT IS FETCHED. Every stored filing of those companies the local copy lacks, from NSE's archive
(nsearchives, the host the rest of zen reads filings from), at most --rate requests a second, each
kept in the local copy once read.

WHAT IS WRITTEN. Only the column excise_duty, in every data/financials file, matched on the filing's
own address (xbrl_url); a file without the column gains it, empty except where a duty was read. A
filing is given a duty only when re-reading it returns the revenue stored for it (the same quarter,
the same document); otherwise it is left empty and listed. Every other column of every file is
checked unchanged before anything is written. The report (data/financials_staging/excise_report.csv)
lists each filing: duty read, none in the filing, revenue mismatch, or not fetched."""

from __future__ import annotations

import argparse
import logging
import math
import os
import time
from pathlib import Path

import duckdb
import pandas as pd
import requests

from zen.data import financials as fin
from zen.data import xbrl_cache

log = logging.getLogger(__name__)
REPORT = Path("data/financials_staging/excise_report.csv")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "*/*"}


def companies(con) -> list[str]:
    """Symbols with at least one locally held filing that reads an excise duty."""
    f = con.execute("SELECT DISTINCT symbol, xbrl_url FROM financials WHERE xbrl_url IS NOT NULL").df()
    out = set()
    for r in f.itertuples():
        if r.symbol in out:
            continue
        b = xbrl_cache.get(r.xbrl_url)
        if b is not None and (b"xcise" in b or b"XCISE" in b) and fin.parse_xbrl(b).get("excise_duty") is not None:
            out.add(r.symbol)
    return sorted(out)


def document(s, url: str, rate: float) -> bytes | None:
    b = xbrl_cache.get(url)
    if b is not None:
        return b
    time.sleep(1.0 / rate)
    for attempt in range(3):
        try:
            r = s.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            xbrl_cache.put(url, r.content)          # keeps only a well-formed document
            return r.content if xbrl_cache.get(url) is not None else None
        if r.status_code == 404:
            return None
        time.sleep(5 * (attempt + 1))
    return None


def same(a, b) -> bool:
    if a is None or b is None or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
        return (a is None or (isinstance(a, float) and math.isnan(a))) and (b is None or (isinstance(b, float) and math.isnan(b)))
    return abs(float(a) - float(b)) <= max(1.0, 1e-9 * abs(float(b)))


def read_duties(con, symbols: list[str], rate: float) -> pd.DataFrame:
    rows = con.execute("SELECT symbol, period_end, consolidated, revenue, xbrl_url FROM financials "
                       "WHERE symbol IN (SELECT unnest(?)) AND xbrl_url IS NOT NULL", [symbols]).df()
    s = requests.Session()
    s.headers.update(UA)
    out = []
    for i, r in enumerate(rows.itertuples(), 1):
        b = document(s, r.xbrl_url, rate)
        if b is None:
            out.append({"xbrl_url": r.xbrl_url, "symbol": r.symbol, "period_end": r.period_end, "outcome": "not fetched", "excise_duty": None})
            continue
        facts = fin.parse_xbrl(b)
        if not same(facts.get("revenue"), r.revenue):
            out.append({"xbrl_url": r.xbrl_url, "symbol": r.symbol, "period_end": r.period_end, "outcome": "revenue mismatch",
                        "excise_duty": None, "stored_revenue": r.revenue, "read_revenue": facts.get("revenue")})
            continue
        duty = facts.get("excise_duty")
        out.append({"xbrl_url": r.xbrl_url, "symbol": r.symbol, "period_end": r.period_end,
                    "outcome": "duty" if duty is not None else "none in the filing", "excise_duty": duty})
        if i % 200 == 0:
            log.info("%s of %s filings read", i, len(rows))
    return pd.DataFrame(out)


def write(duties: pd.DataFrame, root: Path = fin.PARQUET_DIR) -> int:
    """excise_duty into every statement file, matched on xbrl_url; every other column checked unchanged."""
    by_url = duties.dropna(subset=["excise_duty"]).drop_duplicates("xbrl_url").set_index("xbrl_url")["excise_duty"]
    n = 0
    for p in fin.statement_files(root):
        df = pd.read_parquet(p)
        before = df.drop(columns=["excise_duty"], errors="ignore").copy()
        col = df["excise_duty"] if "excise_duty" in df else pd.Series(float("nan"), index=df.index)
        new = df["xbrl_url"].map(by_url)
        col = new.where(new.notna(), col).astype("float64")
        out = before.copy()
        out["excise_duty"] = col.to_numpy()
        tmp = p.with_name(p.stem + ".excise_tmp.parquet")
        out.to_parquet(tmp, index=False, compression="zstd")
        back = pd.read_parquet(tmp)
        # the file as written holds every row and every other column exactly as it was
        pd.testing.assert_frame_equal(back.drop(columns=["excise_duty"]), before)
        os.replace(tmp, p)
        n += int(new.notna().sum())
    return n


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rate", type=float, default=3.0, help="requests a second to nsearchives")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    con = duckdb.connect("data/zen.duckdb", read_only=True)
    syms = companies(con)
    log.info("%s companies file an excise line: %s", len(syms), ", ".join(syms))
    duties = read_duties(con, syms, a.rate)
    con.close()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    duties.to_csv(REPORT, index=False)
    log.info("outcomes: %s", duties["outcome"].value_counts().to_dict())
    if not a.dry_run:
        log.info("written: %s filings given a duty", write(duties))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

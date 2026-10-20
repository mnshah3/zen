"""Balance sheets and cash flows from BSE's own XBRL result filings, for the half years NSE's XBRL
carries none of (before Sep 2022). Research data, kept on the machine that fetched it (BSE's terms;
zen-portfolios, private, copies what its pages need): data/bse_statements/<BSE code>.parquet.

    python -m jobs.update_bse_statements                  # Sep 2018 to Mar 2022, every company zen prices
    python -m jobs.update_bse_statements --from 2018-03 --to 2022-06

WHY BSE, AND HOW IT WAS CHECKED (20 Oct 2026). NSE's results XBRL tags no balance sheet before Sep
2022. BSE's filings of the same results do (Result_Arch_ng lists each filing's standalone and
consolidated XBRL: stand_xbrl_link, conso_xbrl_link). Read with zen's own parser
(zen.data.statements.parse), 192 company-periods of 30 companies from Sep 2022 to Mar 2024 agreed with
zen's NSE statements on every line compared (equity, owners' equity, assets, current assets, cash,
borrowings, receivables, inventories, plant) within 0.5%, all of them exactly; Titan's owners'
equity for Mar 2019 to Mar 2022 (6,070, 6,669, 7,497, 9,303 crore) equals Tapetide's capital plus
reserves. Cash flows are tagged from the year to Mar 2021.

WHICH FILINGS. A company's balance sheet is filed with its half-year and year-end results: the March
and September filings of a March year, June and December of a December year (a company whose BSE
list shows a year ending in December). Both bases where filed. A filing BSE gives no time for is
dated by the next later filing that has one (results are filed in order), never earlier.
BSE refuses GitHub's runners; this runs from a home connection, once, and stops after five failures
in a row so a refusing BSE is not pressed (run it again to resume; stored filings are skipped)."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import duckdb
import pandas as pd
import requests

from jobs.update_bse_results import isin_codes
from zen.data import bse_announcements, bse_results, statements

log = logging.getLogger(__name__)
DIR = Path("data/bse_statements")
HOST = "https://www.bseindia.com"


def targets(con) -> list[int]:
    """BSE codes of every company zen prices since 2018, matched on every ISIN it traded under."""
    isin = con.execute("SELECT DISTINCT isin_code AS isin FROM prices WHERE date >= '2018-01-01' "
                       "AND series IN ('EQ', 'BE')").df()
    return sorted({int(c) for c in isin.merge(isin_codes(), on="isin")["scrip_cd"]})


def filings(table: list[dict], lo: pd.Period, hi: pd.Period) -> pd.DataFrame:
    """[url, consolidated, period_end, filed_dt] of the half-year and year-end filings between lo and
    hi, one row a document."""
    rows = []
    for a in table or []:
        q = (a.get("Quarter") or "").split(";")[0]
        if "-" not in q:
            continue
        basis, label = q.split("-", 1)
        try:
            pe = pd.Period(pd.to_datetime(label, format="%b-%y"), "M")
        except ValueError:
            continue
        cons = basis == "Consolidated"
        link = a.get("conso_xbrl_link") if cons else a.get("stand_xbrl_link")
        filed = a.get("Filing_Date_Time")
        rows.append({"url": link, "consolidated": cons, "period": pe, "kind": a.get("Fld_QuarterName") or "",
                     "filed_dt": pd.Timestamp(filed) if filed else pd.NaT})
    df = pd.DataFrame(rows, columns=["url", "consolidated", "period", "kind", "filed_dt"])
    if df.empty:
        return df
    # results are filed in order: an undated filing is dated by the next later dated one
    df = df.sort_values(["period", "filed_dt"])
    df["filed_dt"] = df["filed_dt"].bfill()
    year_months = {p.month for p in df.loc[df["kind"] == "Year", "period"]} or {3}
    half = {(m - 1 + 6) % 12 + 1 for m in year_months} | year_months
    df = df[df["url"].notna() & df["period"].map(lambda p: p.month in half) & (df["period"] >= lo) & (df["period"] <= hi)]
    df = df.drop_duplicates("url")
    df["period_end"] = df["period"].map(lambda p: p.end_time.normalize())
    return df[["url", "consolidated", "period_end", "filed_dt"]]


def path(code: int) -> Path:
    return DIR / f"{code}.parquet"


def fetch_company(s, code: int, lo, hi, pause: float = 1.0) -> int:
    arch = s.get(f"{bse_results.API}/Result_Arch_ng/w", params={"scrip_cd": code}, timeout=60)
    arch.raise_for_status()
    todo = filings((arch.json() or {}).get("Table") or [], lo, hi)
    time.sleep(pause)
    p = path(code)
    have = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    # a finance company's filing read in the general layout before 20 Oct 2026 evening is read again
    ok = ~(have["url"].map(statements.is_nbfc) & (have["format"] != "nbfc")) if len(have) else None
    done = set(have.loc[ok, "url"]) if len(have) else set()
    have = have[ok] if len(have) else have
    rows = []
    for f in todo.itertuples():
        if f.url in done:
            continue
        r = s.get(HOST + f.url, timeout=90)
        time.sleep(pause)
        if r.status_code != 200 or not r.content.lstrip()[:1] == b"<":
            continue
        row = statements.parse(r.content, nbfc=statements.is_nbfc(f.url))   # a finance company's own layout
        if not row.get("has_detail") and not row.get("has_cash_flow"):
            continue
        rows.append({"scrip_cd": code, "url": f.url, "consolidated": f.consolidated, "period_end": f.period_end,
                     "filed_dt": f.filed_dt, **row})
    if rows:
        DIR.mkdir(parents=True, exist_ok=True)
        df = pd.concat([have, pd.DataFrame(rows)], ignore_index=True) if len(have) else pd.DataFrame(rows)
        df.drop_duplicates("url", keep="last").to_parquet(p, index=False, compression="zstd")
    return len(rows)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="lo", default="2018-03")
    ap.add_argument("--to", dest="hi", default="2022-06")
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--codes", help="a file of BSE codes in the order to read them (the rest follow)")
    a = ap.parse_args(argv)
    con = duckdb.connect("data/zen.duckdb", read_only=True)
    codes = targets(con)
    con.close()
    if a.codes:
        first = [int(x) for x in Path(a.codes).read_text().split()]
        codes = list(dict.fromkeys([c for c in first if c in set(codes)] + codes))
    lo, hi = pd.Period(a.lo, "M"), pd.Period(a.hi, "M")
    log.info("%s companies, %s to %s", len(codes), lo, hi)
    s = requests.Session()
    s.headers.update(bse_announcements.HEADERS)
    failures = done = rows = 0
    for code in codes:
        try:
            rows += fetch_company(s, code, lo, hi, a.pause)
            failures = 0
        except Exception as e:                                       # noqa: BLE001
            failures += 1
            log.warning("%s: %s", code, e)
            if failures >= 5:
                log.error("five failures in a row: stopping; run again later to resume")
                return 1
            time.sleep(30 * failures)
        done += 1
        if done % 50 == 0:
            log.info("%s of %s companies, %s filings stored", done, len(codes), rows)
    log.info("done: %s companies, %s filings stored", done, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

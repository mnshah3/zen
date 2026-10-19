"""BSE's standalone quarterly results into data/bse_results (zen/data/bse_results.py says what and why).
Run from a home connection (BSE refuses GitHub's runners); one request a second; resumable.

    python -m jobs.update_bse_results --gaps                       # what NSE's XBRL lacks since 2018
    python -m jobs.update_bse_results --history 2013-01 2018-03     # every NSE company, those quarters

--gaps: the quarters a company's NSE record skips inside its own span (2,414 on 19 Oct 2026), and every
quarter since 2018 of the companies NSE carries no results for (BSE-listed, permitted to trade on NSE).
--history: every company that traded on NSE in the window, delisted ones included, matched to its BSE
code by ISIN; its quarters (and years) ending in the window.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date

import duckdb
import pandas as pd
import requests

from zen.data import bse_announcements, bse_results

log = logging.getLogger("update_bse_results")
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def label_end(label: str) -> pd.Period | None:
    """'Jun-16' -> 2016-06 (the month the period ends)."""
    try:
        m, y = label.split("-")[:2]
        return pd.Period(year=2000 + int(y[-2:]), month=MONTHS[m[:3]], freq="M")
    except (ValueError, KeyError):
        return None


def isin_codes() -> pd.DataFrame:
    s = pd.read_parquet("data/reference/bse_scrips.parquet")[["isin", "scrip_cd"]].dropna()
    return s.drop_duplicates("isin")


def gap_targets(con) -> dict[int, set]:
    """{scrip: {period-end months}}: NSE-missing quarters since 2018, and BSE-only companies' quarters."""
    q = con.execute("SELECT DISTINCT symbol, period_end FROM financials").df()
    q["p"] = pd.to_datetime(q["period_end"]).dt.to_period("Q")
    isin = con.execute("SELECT symbol, any_value(isin_code) AS isin FROM prices WHERE date >= '2018-01-01' "
                       "AND series IN ('EQ', 'BE') GROUP BY symbol").df()
    codes = isin.merge(isin_codes(), on="isin")
    out: dict[int, set] = {}
    for sym, g in q.groupby("symbol"):
        span = pd.period_range(g["p"].min(), g["p"].max(), freq="Q")
        miss = set(span) - set(g["p"])
        code = codes.loc[codes["symbol"] == sym, "scrip_cd"]
        if miss and len(code):
            out[int(code.iloc[0])] = {p.asfreq("M", "end") for p in miss}
    # companies NSE carries no results for, if they ever met zen's liquidity rule (a year's median
    # daily turnover of at least Rs 10 lakh, research/export.MIN_TURNOVER): the rest never reach a page
    liquid = set(con.execute("SELECT symbol FROM (SELECT symbol, year(date) y, median(turnover) t FROM prices "
                             "WHERE date >= '2018-01-01' AND series IN ('EQ', 'BE') GROUP BY 1, 2) WHERE t >= 1e6").df()["symbol"])
    without = codes[~codes["symbol"].isin(set(q["symbol"])) & codes["symbol"].isin(liquid)]
    for c in without["scrip_cd"]:
        out[int(c)] = None                                   # every quarter since 2018
    return out


def history_targets(con, start: str, end: str) -> list[int]:
    isin = con.execute("SELECT DISTINCT isin_code AS isin FROM prices WHERE date BETWEEN ? AND ? AND series IN ('EQ', 'BE')",
                       [pd.Timestamp(start + "-01").date(), (pd.Period(end) + 1).start_time.date()]).df()
    return sorted(int(c) for c in isin.merge(isin_codes(), on="isin")["scrip_cd"])


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--gaps", action="store_true")
    g.add_argument("--history", nargs=2, metavar=("FROM", "TO"))
    ap.add_argument("--pause", type=float, default=1.0)
    a = ap.parse_args(argv)
    con = duckdb.connect("data/zen.duckdb", read_only=True)
    if a.gaps:
        targets = gap_targets(con)
        since = pd.Period("2018-01", "M")
        jobs = [(c, (lambda row, m=months: (lambda p: p is not None and p >= since and (m is None or p in m))(label_end(row.label))))
                for c, months in sorted(targets.items())]
    else:
        lo, hi = pd.Period(a.history[0], "M"), pd.Period(a.history[1], "M")
        jobs = [(c, lambda row: (lambda p: p is not None and lo <= p <= hi)(label_end(row.label)))
                for c in history_targets(con, a.history[0], a.history[1])]
    con.close()
    log.info("%s companies to read", len(jobs))
    s = requests.Session()
    s.headers.update(bse_announcements.HEADERS)
    failures, done, rows = 0, 0, 0
    for code, want in jobs:
        try:
            rows += bse_results.fetch_company(s, code, want, pause=a.pause)
            failures = 0
        except Exception as e:                                # noqa: BLE001
            failures += 1
            log.warning("%s: %s", code, e)
            if failures >= 5:                                 # BSE refusing: stop rather than press on
                log.error("five failures in a row: stopping; run again later to resume")
                return 1
            time.sleep(30 * failures)
        done += 1
        if done % 50 == 0:
            log.info("%s of %s companies, %s quarters stored", done, len(jobs), rows)
    log.info("done: %s companies, %s quarters stored", done, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""BSE's standalone quarterly results into data/bse_results (zen/data/bse_results.py says what and why).
Run from a home connection (BSE refuses GitHub's runners); one request a second; resumable.

    python -m jobs.update_bse_results --gaps                       # what NSE's XBRL lacks since 2018
    python -m jobs.update_bse_results --history 2013-01 2018-03     # every NSE company, those quarters

--gaps: the quarters since 2018 a company's NSE record lacks, and every quarter since 2018 of the
companies NSE carries no results for (BSE-listed, permitted to trade on NSE).
--history: every company that traded on NSE in the window, delisted ones included, matched to its BSE
code by ISIN; its quarters (and years) ending in the window.
--codes FILE: with --gaps, only the BSE codes in FILE (whitespace-separated), in that order.

WHAT --gaps MISSED UNTIL 20 NOV 2026, measured against the research pages of that night:
  * a BSE-only company was read only if it had once met a turnover of Rs 10 lakh a day. The pages
    dropped that floor on 25 Oct 2026 (zen-portfolios' research/export.MIN_TURNOVER is 0: every
    company traded in the last week), so 131 of the companies they list had no results at all, among
    them those NSE admitted to trade on 20 Apr and 17 Aug 2026. Every company traded in the last 90
    days is read now, whatever its turnover.
  * a company's NSE gaps were counted only between its first and last NSE quarter, so one whose NSE
    filings stop (filed on BSE alone since) or start late was never asked for the rest. They are
    counted now from its first NSE session (or March 2018) to the latest quarter due, while it trades.
  * a filing NSE holds without a quarter's figures (a half year or a year only) counted as held.
"""

from __future__ import annotations

import argparse
import logging
import time

import duckdb
import pandas as pd

from zen.data import bse_results

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


def latest_due(today) -> pd.Period:
    """The latest quarter whose results are due by `today`: 45 days after a quarter ends, 60 after a
    March quarter (SEBI's listing regulations, 33(3)). A company whose year ends in another month has
    60 days for that year's last quarter, which this does not know."""
    q = pd.Period(pd.Timestamp(today), "Q")
    while True:
        end = q.end_time.normalize()
        if (pd.Timestamp(today) - end).days > (60 if end.month == 3 else 45):
            return q
        q -= 1


def gap_targets(con, today=None) -> dict[int, set]:
    """{scrip: {period-end months}, or None for every quarter since 2018}: the quarters since 2018 that
    NSE's record of a company lacks (WHAT --gaps MISSED, above), and BSE-only companies."""
    q = con.execute("SELECT DISTINCT symbol, period_end FROM financials WHERE (revenue IS NOT NULL OR total_income IS NOT NULL "
                    "OR profit_reported IS NOT NULL) AND (quarter_span_days IS NULL OR quarter_span_days <= 100)").df()
    if "bank_results" in set(con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"]):
        q = pd.concat([q, con.execute("SELECT DISTINCT symbol, period_end FROM bank_results WHERE has_figures").df()])
    q["p"] = pd.to_datetime(q["period_end"]).dt.to_period("Q")
    # every ISIN a symbol has traded under: a split or bonus changes it (MCX: INE745G01035 became
    # INE745G01043), and BSE's list carries only the current one
    isin = con.execute("SELECT DISTINCT symbol, isin_code AS isin FROM prices WHERE date >= '2018-01-01' "
                       "AND series IN ('EQ', 'BE')").df()
    codes = isin.merge(isin_codes(), on="isin").drop_duplicates("symbol")
    asof = con.execute("SELECT max(date) FROM prices").fetchone()[0]
    px = con.execute("SELECT symbol, min(date) AS first, max(date) AS last FROM prices WHERE series IN ('EQ', 'BE') "
                     "GROUP BY 1").df().set_index("symbol")
    trading = set(px.index[pd.to_datetime(px["last"]) >= pd.Timestamp(asof) - pd.Timedelta(days=90)])
    due = latest_due(today or asof)
    since = pd.Period("2018-03", "Q")
    out: dict[int, set] = {}
    for sym, g in q.groupby("symbol"):
        code = codes.loc[codes["symbol"] == sym, "scrip_cd"]
        if not len(code):
            continue
        first = pd.Period(pd.Timestamp(px.at[sym, "first"]), "Q") if sym in px.index else since
        hi = max(g["p"].max(), due) if sym in trading else g["p"].max()
        miss = set(pd.period_range(max(since, min(first, g["p"].min())), hi, freq="Q")) - set(g["p"])
        if miss:
            out[int(code.iloc[0])] = {p.asfreq("M", "end") for p in miss}
    # companies NSE carries no results for: the ones that once met a turnover of Rs 10 lakh a day, and
    # every one still trading (the pages list every company traded in the last week)
    liquid = set(con.execute("SELECT symbol FROM (SELECT symbol, year(date) y, median(turnover) t FROM prices "
                             "WHERE date >= '2018-01-01' AND series IN ('EQ', 'BE') GROUP BY 1, 2) WHERE t >= 1e6").df()["symbol"])
    # (a BSE code is one company under every symbol it traded as: TATAGLOBAL, which NSE holds no results
    # under, is TATACONSUM's 500800, so a code with results under any of its symbols is not BSE-only)
    with_results = set(codes.loc[codes["symbol"].isin(set(q["symbol"])), "scrip_cd"])
    without = codes[~codes["scrip_cd"].isin(with_results) & codes["symbol"].isin(liquid | trading)]
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
    ap.add_argument("--codes", help="with --gaps: a file of the BSE codes to read, in that order (the others are left)")
    a = ap.parse_args(argv)
    con = duckdb.connect("data/zen.duckdb", read_only=True)
    if a.gaps:
        targets = gap_targets(con)
        order = sorted(targets)
        if a.codes:
            order = [c for c in dict.fromkeys(int(x) for x in open(a.codes).read().split()) if c in targets]
        since = pd.Period("2018-01", "M")
        # quarters only (BSE's whole quarter numbers): the half years, nine months and years beside them
        # are not what a gap is, and asking for them too took two requests in five (20 Nov 2026)
        jobs = [(c, (lambda row, m=targets[c]: row.qtr % 1 == 0 and (lambda p: p is not None and p >= since and (m is None or p in m))(label_end(row.label))))
                for c in order]
    else:
        lo, hi = pd.Period(a.history[0], "M"), pd.Period(a.history[1], "M")
        jobs = [(c, lambda row: (lambda p: p is not None and lo <= p <= hi)(label_end(row.label)))
                for c in history_targets(con, a.history[0], a.history[1])]
    con.close()
    log.info("%s companies to read", len(jobs))
    s = bse_results.session()
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

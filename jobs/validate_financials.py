"""Is the fundamentals panel actually usable? Checks, not assurances.

Run before anything is built on this data. Every check either prints a number
that can be argued with or fails loudly; none of them report "OK" without
showing the evidence for it.

The checks exist because each corresponds to a way this archive could be
quietly wrong:

  coverage      a year with half the companies is not a year
  fill          a column present but null is worse than absent, because it
                silently shrinks a sample rather than raising
  point-in-time a filing whose broadcast_dt precedes its period_end is
                impossible and means the timestamps cannot be trusted at all
  duplicates    the same filing twice inflates a count and can double-weight
                one company in a cross-section
  identity      one symbol carrying two ISINs is a rename or a data error, and
                joins on symbol will silently mix two companies
  spot values   figures checked against what the company actually reported --
                the only check that catches a systematically wrong parser

    python -m jobs.validate_financials
"""

from __future__ import annotations

import logging
import sys

import duckdb
import pandas as pd

log = logging.getLogger(__name__)
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'  ' + detail if detail else ''}")
    if not ok:
        FAIL.append(name)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    pd.set_option("display.width", 210)
    con = duckdb.connect()
    con.execute("""CREATE VIEW fin AS SELECT * FROM
                   read_parquet('data/financials/legacy_2*.parquet', union_by_name=true)""")
    con.execute("""CREATE VIEW idx AS SELECT * FROM
                   read_parquet('data/financials/legacy_index.parquet')""")

    print("\n=== 1. SHAPE ===")
    n, cos, lo, hi = con.execute(
        "SELECT count(*), count(DISTINCT symbol), min(period_end), max(period_end) FROM fin"
    ).fetchone()
    print(f"  {n:,} rows | {cos:,} companies | {str(lo)[:10]} to {str(hi)[:10]}")

    print("\n=== 2. COVERAGE BY YEAR ===")
    cov = con.execute("""
        SELECT year(period_end) AS y, count(*) AS rows,
               count(DISTINCT symbol) AS companies,
               sum(CASE WHEN consolidated THEN 1 ELSE 0 END) AS consol,
               round(100.0 * count(revenue) / count(*), 1) AS pct_revenue,
               round(100.0 * count(equity) / count(*), 1) AS pct_balance_sheet
        FROM fin GROUP BY 1 ORDER BY 1""").df()
    print(cov.to_string(index=False))
    full = cov[cov.y.between(2019, 2024)]
    check("every year 2019-2024 has >1,200 companies",
          bool((full.companies > 1200).all()),
          f"min {int(full.companies.min())}")

    print("\n=== 3. FIELD FILL RATES ===")
    cols = ["revenue", "total_income", "ebitda", "pbt", "profit_reported",
            "profit_normalised", "eps_basic", "equity", "assets", "debt_total",
            "debt_to_equity"]
    fill = con.execute(
        "SELECT " + ", ".join(f"round(100.0*count({c})/count(*),1) AS {c}" for c in cols)
        + " FROM fin").df().T
    fill.columns = ["pct_present"]
    print(fill.to_string())
    check("revenue present on >95% of rows",
          float(fill.loc["revenue", "pct_present"]) > 95)
    check("EBITDA present on >90% of rows",
          float(fill.loc["ebitda", "pct_present"]) > 90)

    print("\n=== 4. POINT-IN-TIME INTEGRITY ===")
    bad = con.execute("""SELECT count(*) FROM fin
        WHERE broadcast_dt IS NOT NULL
          AND CAST(broadcast_dt AS DATE) < CAST(period_end AS DATE)""").fetchone()[0]
    check("no filing broadcast before its period ended", bad == 0, f"{bad:,} violations")

    lag = con.execute("""SELECT
        median(date_diff('day', CAST(period_end AS DATE), CAST(broadcast_dt AS DATE))) AS med,
        quantile_cont(date_diff('day', CAST(period_end AS DATE), CAST(broadcast_dt AS DATE)), 0.95) AS p95
        FROM fin WHERE broadcast_dt IS NOT NULL""").fetchone()
    print(f"  reporting lag: median {lag[0]:.0f} days, 95th pct {lag[1]:.0f} days")
    check("median reporting lag between 25 and 75 days",
          25 <= float(lag[0]) <= 75, "SEBI allows 45 days for a quarter")

    missing = con.execute("SELECT count(*) FROM fin WHERE broadcast_dt IS NULL").fetchone()[0]
    check("every row carries a broadcast timestamp", missing == 0, f"{missing:,} null")

    print("\n=== 5. REVISIONS ===")
    # Not duplicates. A company files a quarter and then files it again --
    # sometimes byte-identical (a re-broadcast: ACC filed Q2 FY24 on 27 and
    # again on 30 October), sometimes with revised figures. Both belong in a
    # point-in-time archive, because on any given date you saw whichever
    # version had been published by then. The rule is "latest broadcast_dt on
    # or before the as-of date", and what is worth checking is that the rule
    # resolves to exactly one row -- not that revisions are absent, which would
    # mean the archive had thrown history away.
    dup, revised = con.execute("""
        WITH g AS (SELECT symbol, period_end, consolidated, count(*) AS n,
                          count(DISTINCT revenue) AS rv
                   FROM fin GROUP BY 1,2,3 HAVING n > 1)
        SELECT count(*), sum(CASE WHEN rv > 1 THEN 1 ELSE 0 END) FROM g""").fetchone()
    revised = int(revised or 0)
    print(f"  {dup:,} keys filed more than once "
          f"({revised:,} with revised figures, "
          f"{dup - revised:,} re-broadcast unchanged)")
    unresolved = con.execute("""
        WITH latest AS (
            SELECT symbol, period_end, consolidated,
                   row_number() OVER (PARTITION BY symbol, period_end, consolidated
                                      ORDER BY broadcast_dt DESC, xbrl_url DESC) AS rn
            FROM fin)
        SELECT count(*) FROM (
            SELECT symbol, period_end, consolidated, count(*) AS c
            FROM latest WHERE rn = 1 GROUP BY 1,2,3 HAVING c > 1)""").fetchone()[0]
    check("latest-broadcast rule resolves every key to one row", unresolved == 0,
          f"{unresolved:,} still ambiguous")

    print("\n=== 6. INTERNAL CONSISTENCY ===")
    # revenue + other_income = total_income is the identity that must hold.
    # An earlier check asserted revenue <= total_income, which is false whenever
    # other income is NEGATIVE -- a fair-value loss or a write-back. SHAH
    # reported revenue 3.7cr, other income -3.2cr, total 0.5cr: arithmetically
    # perfect and flagged as corrupt.
    incons = con.execute("""SELECT count(*) FROM fin
        WHERE revenue IS NOT NULL AND total_income IS NOT NULL
          AND other_income IS NOT NULL
          AND abs(revenue + other_income - total_income) > 0.02 * abs(total_income) + 1e5
        """).fetchone()[0]
    tot_ti = con.execute("SELECT count(*) FROM fin WHERE total_income IS NOT NULL").fetchone()[0]
    check("revenue + other income reconciles to total income",
          incons < 0.01 * tot_ti, f"{incons:,} of {tot_ti:,} do not")

    # Negative revenue is legitimate and rare: trading houses and financials
    # book reversals and fair-value losses through the top line. MMTC, IFCI and
    # Edelweiss account for most of them. It is worth watching the RATE, since
    # a parser fault would push it far higher, but zero would be wrong.
    neg, tot_rev = con.execute(
        "SELECT count(*) FILTER (WHERE revenue < 0), count(revenue) FROM fin").fetchone()
    check("negative revenue stays under 0.5% of rows", neg < 0.005 * tot_rev,
          f"{neg:,} of {tot_rev:,} ({100*neg/tot_rev:.2f}%)")

    bs = con.execute("""SELECT count(*) FROM fin
        WHERE assets IS NOT NULL AND equity IS NOT NULL AND liabilities IS NOT NULL
          AND abs(assets - (equity + liabilities)) > 0.02 * assets""").fetchone()[0]
    tot_bs = con.execute("SELECT count(*) FROM fin WHERE assets IS NOT NULL").fetchone()[0]
    check("balance sheets balance within 2%", bs < max(20, 0.02 * tot_bs),
          f"{bs:,} of {tot_bs:,} do not")

    print("\n=== 7. COVERAGE AGAINST THE INDEX ===")
    got, want = con.execute("""
        SELECT (SELECT count(DISTINCT xbrl_url) FROM fin),
               (SELECT count(*) FROM idx WHERE has_xbrl)""").fetchone()
    print(f"  parsed {got:,} of {want:,} documents ({100*got/want:.1f}%)")
    check("at least 98% of available documents parsed", got / want >= 0.98)

    print("\n=== 8. SPOT CHECK -- real figures, checkable against the filings ===")
    spot = con.execute("""
        SELECT symbol, CAST(period_end AS DATE) AS period, consolidated,
               round(revenue/1e7, 0) AS revenue_cr,
               round(ebitda/1e7, 0) AS ebitda_cr,
               round(profit_normalised/1e7, 0) AS pat_cr,
               eps_basic, round(debt_to_equity, 3) AS de
        FROM fin
        WHERE symbol IN ('RELIANCE','TCS','INFY','HDFCBANK','SUZLON')
          AND period_end IN (DATE '2023-03-31', DATE '2024-03-31')
        ORDER BY symbol, period, consolidated""").df()
    print(spot.to_string(index=False))
    print("\n  Check a few of these against the company's own filing before "
          "trusting the panel. Figures are in Rs crore.")

    con.close()
    print("\n" + "=" * 70)
    if FAIL:
        print(f"{len(FAIL)} CHECK(S) FAILED: {', '.join(FAIL)}")
        print("The panel is NOT safe to build on until these are explained.")
        return 1
    print("All checks passed. Boundaries still apply: income statement from "
          "2018, balance sheet from Sep 2022.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

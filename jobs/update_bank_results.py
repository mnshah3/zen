"""Read lenders' quarterly results from their banking-format filings (zen.data.bank_results).

    python -m jobs.update_bank_results              # every lender filing not yet read
    python -m jobs.update_bank_results --limit 50   # at most 50 documents this run

The documents are the ones `financials` already lists for lenders, so run this after the
financials update. Optional research data: nothing in zen's strategies reads it, and a
failure is reported and exits non-zero without touching anything else.
"""

from __future__ import annotations

import argparse
import logging

from zen.data import bank_results, store
from zen.data.bhavcopy import _session


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int)
    a = p.parse_args(argv)

    con = store.connect()
    try:
        docs = bank_results.documents(con)
        have = bank_results.stored_urls()
        todo = docs[~docs["xbrl_url"].isin(have)]
        if a.limit:
            todo = todo.tail(a.limit)                 # the newest first when limited
        rows, failed = bank_results.fetch(todo, _session() if len(todo) else None)
        bank_results.write(rows)
        n = bank_results.rebuild(con)
        tot = con.execute("SELECT count(*) FILTER (WHERE has_figures), count(DISTINCT symbol) FILTER (WHERE has_figures), "
                          "min(period_end) FILTER (WHERE has_figures), max(period_end) FILTER (WHERE has_figures) "
                          "FROM bank_results").fetchone()
    finally:
        con.close()
    with_figs = int(rows["has_figures"].sum()) if len(rows) else 0
    print(f"bank results: {len(todo):,} documents to read, {len(rows):,} read ({with_figs:,} with bank figures), "
          f"{failed:,} not fetched | table {n:,} rows, {tot[0]:,} with figures for {tot[1]:,} lenders, "
          f"{tot[2]} to {tot[3]}")
    return 1 if failed and failed == len(todo) else 0


if __name__ == "__main__":
    raise SystemExit(main())

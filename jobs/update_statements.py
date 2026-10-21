"""Read the detailed balance sheet and the cash flow statement from the half-year and year-end
filings (zen.data.statements).

    python -m jobs.update_statements               # every balance-sheet filing not yet read
    python -m jobs.update_statements --limit 50    # at most 50 documents this run, the newest
    python -m jobs.update_statements --latest      # only the last revision of each period
    python -m jobs.update_statements --primary     # with --latest: one basis a company and period,
                                                   # consolidated where it was filed
    python -m jobs.update_statements --reparse     # read again every stored document an older
                                                   # parser read (statements.PARSER_VERSION)
    python -m jobs.update_statements --reparse --full-year   # only the year-end filings
    python -m jobs.update_statements --cache-only  # only documents already in the local copy
    python -m jobs.update_statements --missing-year  # also read again the year-end filings an older
                                                   # parser left without the year's profit
    python -m jobs.update_statements --rows-out F  # also keep this run's rows in F, so they can be
                                                   # merged again after a conflict (--merge-rows F)

The documents are the ones `financials` already lists with a balance sheet, so run this after
the financials update. Optional research data: nothing in zen's strategies reads it, and a
failure is reported and exits non-zero without touching anything else. Documents are read in
batches and each batch is written before the next, so a long first run can be stopped and
started again without losing what it has read.
"""

from __future__ import annotations

import argparse
import logging
import time

import duckdb
import pandas as pd

from zen.data import statements, store

BATCH = 250


def _archive(read_only: bool, tries: int = 30, wait: float = 20.0):
    """The archive, waiting a while if another process has it open (a rebuild, a test run)."""
    for attempt in range(tries):
        try:
            return duckdb.connect(str(store.DB_PATH), read_only=True) if read_only else store.connect()
        except duckdb.IOException as e:
            if attempt == tries - 1:
                raise
            logging.info("statements: the archive is in use, waiting (%s)", str(e).splitlines()[0])
            time.sleep(wait)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int)
    p.add_argument("--latest", action="store_true", help="only the last revision of each (symbol, basis, period)")
    p.add_argument("--primary", action="store_true",
                   help="one basis for each company and period: consolidated where filed, else standalone")
    p.add_argument("--reparse", action="store_true",
                   help="read every document again, from the local cache where it is there")
    p.add_argument("--full-year", action="store_true",
                   help="only documents whose stored year to date is a full year (360 to 370 days)")
    p.add_argument("--cache-only", action="store_true", help="only documents already in the local copy")
    p.add_argument("--missing-year", action="store_true",
                   help="besides the documents not yet read, the year-end ones an older parser left without the year's profit")
    p.add_argument("--rows-out", help="also write this run's rows to this parquet file")
    p.add_argument("--merge-rows", help="merge the rows in this parquet file into data/statements and stop")
    p.add_argument("--pause", type=float, default=0.35, help="seconds between downloads")
    a = p.parse_args(argv)
    if a.merge_rows:
        rows = pd.read_parquet(a.merge_rows)
        statements.write(rows)
        print(f"statements: {len(rows):,} rows merged")
        return 0

    # The archive is opened only to list the documents and, at the end, to rebuild the table: a
    # first run takes hours, and an open connection would lock the archive for all of it.
    con = _archive(read_only=True)
    try:
        docs = statements.documents(con, latest=a.latest or a.primary)
    finally:
        con.close()
    if a.primary:
        docs = (docs.sort_values(["consolidated", "broadcast_dt"])
                    .drop_duplicates(["symbol", "period_end"], keep="last").sort_values("broadcast_dt"))
    if a.reparse or a.full_year:
        st = statements.stored_versions()
        if a.reparse:
            older = st[st["parser"] < statements.PARSER_VERSION]
            todo = docs[docs["xbrl_url"].isin(older["xbrl_url"])]
        else:
            todo = docs
        if a.full_year:
            full = pd.to_numeric(st["ytd_days"], errors="coerce").between(360, 370)
            todo = todo[todo["xbrl_url"].isin(st.loc[full, "xbrl_url"])]
    else:
        todo = docs[~docs["xbrl_url"].isin(statements.stored_urls())]
        if a.missing_year:
            # PARSER_VERSION 3 reads the year's income where no cash flow dates it: the year-end
            # filings to Mar 2021 a version 2 run stored without it are read again, once
            st = statements.stored_versions()
            year = pd.to_numeric(st["ytd_days"], errors="coerce").between(360, 370)
            again = st[year & ~st["has_year"].astype(bool) & (st["parser"] < statements.PARSER_VERSION)]
            todo = pd.concat([todo, docs[docs["xbrl_url"].isin(again["xbrl_url"])]]).sort_values("broadcast_dt", kind="stable")
    if a.cache_only:
        todo = todo[[statements.xbrl_cache.get(u) is not None for u in todo["xbrl_url"]]]
    # Newest first, always: old filings NSE no longer serves stay unread, and read first they
    # would end each run (fetch stops after a run of failures) before the new ones were reached.
    todo = todo.iloc[::-1]
    if a.limit:
        todo = todo.head(a.limit)
    read = with_cf = failed = 0
    kept = []
    for at in range(0, len(todo), BATCH):
        batch = todo.iloc[at:at + BATCH]
        rows, missed = statements.fetch(batch, pause=a.pause)
        statements.write(rows)
        if a.rows_out and len(rows):
            kept.append(rows)
            pd.concat(kept, ignore_index=True).to_parquet(a.rows_out, index=False)
        read += len(rows)
        failed += missed
        with_cf += int(rows["has_cash_flow"].sum()) if len(rows) else 0
        logging.info("statements: %d of %d documents done", min(at + BATCH, len(todo)), len(todo))
        if len(rows) + missed < len(batch):       # fetch gave up: NSE is refusing
            break
    con = _archive(read_only=False)                # what was read is already in data/statements
    try:
        n = statements.rebuild(con)
        tot = con.execute("SELECT count(*) FILTER (WHERE has_cash_flow), count(DISTINCT symbol), "
                          "min(period_end), max(period_end) FROM statements").fetchone()
    finally:
        con.close()
    print(f"statements: {len(todo):,} documents to read, {read:,} read ({with_cf:,} with a cash flow), "
          f"{failed:,} not fetched | table {n:,} rows, {tot[0]:,} with a cash flow for {tot[1]:,} companies, "
          f"{tot[2]} to {tot[3]}")
    return 1 if len(todo) and read == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

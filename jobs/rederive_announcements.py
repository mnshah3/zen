"""Re-derive `category` from NSE's subtype, and add the session a filing can act on.

TWO COLUMNS, TWO DIFFERENT BUGS.

`category` in the stored parquet is still the output of the old regex, which
matched free text and produced an orders bucket of 14,955 filings, about a
quarter of them regulatory penalties carrying the opposite sign. Every consumer
reading that column is reading the wrong thing. zen/data/filing_types.py now
derives categories from the exchange's own label; this writes that back.

`session_date` is new and fixes a quieter problem. `trade_date` is the first
session a filing COULD affect, computed from the clock alone -- after 15:30 it
rolls to the next calendar weekday. But a weekday is not necessarily a trading
day, and a trading day for the exchange is not necessarily one for the stock:
it may be suspended, newly listed, or simply untraded.

Every study so far joined announcements to prices on equality:

    JOIN prices p ON p.symbol = a.symbol AND p.date = a.trade_date

which silently drops any filing landing on a day that symbol did not trade.
That is 83,977 of 783,510 filings, 10.7%, and it is NOT random -- companies
file before long weekends, so the discarded set is skewed towards exactly the
filings a study would care about. `session_date` rolls forward to the first
session the symbol actually traded, so consumers join on equality and lose
nothing.

    python -m jobs.rederive_announcements            # report only
    python -m jobs.rederive_announcements --apply
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

import duckdb
import pandas as pd

from zen.data.filing_types import categorise

log = logging.getLogger(__name__)


def session_map(con) -> pd.DataFrame:
    """For every (symbol, filing date), the next session that symbol traded.

    Built as an as-of join rather than a lookup table because a symbol's
    trading calendar is its own: suspensions, listings and delistings all make
    it differ from the exchange's.
    """
    return con.execute("""
        SELECT DISTINCT symbol, CAST(date AS DATE) AS session
        FROM read_parquet('data/daily/**/*.parquet', union_by_name=true)
        WHERE isin_code LIKE 'INE%' AND series IN ('EQ','BE')
    """).df()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    pd.set_option("display.width", 200)

    files = sorted(glob.glob("data/announcements/**/*.parquet", recursive=True))
    con = duckdb.connect()

    log.info("building the per-symbol session calendar")
    sess = session_map(con)
    sess["session"] = pd.to_datetime(sess["session"]).astype("datetime64[us]")
    sess = sess.sort_values(["symbol", "session"])
    log.info("  %d symbol-sessions across %d symbols",
             len(sess), sess["symbol"].nunique())

    frames = {f: pd.read_parquet(f) for f in files}
    every = pd.concat(frames.values(), ignore_index=True)
    log.info("announcements: %d rows", len(every))

    old_cat = every["category"].copy() if "category" in every else None
    new_cat = every["subject"].map(categorise)

    # Roll each filing forward to the first session its own symbol traded.
    every["_td"] = pd.to_datetime(every["trade_date"]).astype("datetime64[us]")
    left = every[["symbol", "_td"]].reset_index().sort_values("_td")
    rolled = pd.merge_asof(
        left, sess.rename(columns={"session": "_sd"}).sort_values("_sd"),
        left_on="_td", right_on="_sd", by="symbol", direction="forward")
    rolled = rolled.set_index("index").sort_index()
    every["session_date"] = rolled["_sd"]

    lost = every["session_date"].isna().sum()
    moved = (every["session_date"] != every["_td"]).sum() - lost
    print(f"\nsession_date")
    print(f"  filings landing on a day the symbol traded : "
          f"{len(every) - moved - lost:,}")
    print(f"  rolled forward to the next session         : {moved:,} "
          f"({100*moved/len(every):.1f}%)")
    print(f"  no later session at all (delisted since)   : {lost:,} "
          f"({100*lost/len(every):.1f}%)")

    if old_cat is not None:
        changed = (old_cat != new_cat).sum()
        print(f"\ncategory: {changed:,} of {len(every):,} rows change "
              f"({100*changed/len(every):.1f}%)")
        comp = (pd.crosstab(old_cat, new_cat)
                  .reindex(columns=sorted(new_cat.unique()), fill_value=0))
        print("\nold regex category (rows) vs new subtype category (columns), "
              "top movements:")
        melt = comp.reset_index().melt(id_vars=comp.index.name or "category",
                                       var_name="new", value_name="n")
        melt = melt[melt["n"] > 0].sort_values("n", ascending=False)
        print(melt.head(18).to_string(index=False))

    print(f"\nnew category counts:")
    for k, v in new_cat.value_counts().items():
        print(f"  {k:16} {v:8,}")

    if not args.apply:
        print("\nReport only. Re-run with --apply to write.")
        return 0

    written = 0
    for f, df in frames.items():
        # Recomputed per file rather than sliced out of the concatenation. The
        # concat is for reporting only; slicing it back apart would depend on
        # row order surviving two merges, and a misalignment there would put
        # the right category on the wrong filing without raising anything.
        d = df.copy()
        d["category"] = d["subject"].map(categorise)
        td = pd.to_datetime(d["trade_date"]).astype("datetime64[us]")
        lf = pd.DataFrame({"symbol": d["symbol"], "_td": td}).reset_index().sort_values("_td")
        rl = pd.merge_asof(lf, sess.rename(columns={"session": "_sd"}).sort_values("_sd"),
                           left_on="_td", right_on="_sd", by="symbol", direction="forward")
        d["session_date"] = rl.set_index("index").sort_index()["_sd"]
        d.to_parquet(f, index=False, compression="zstd")
        written += len(d)
    print(f"\napplied: {written:,} rows rewritten across {len(frames)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

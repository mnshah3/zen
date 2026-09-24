"""Fetch NSE's four-level industry labels for v2's sector cap (spec item 7).

    python -m jobs.fetch_industry                 # fetch (resumable), then report
    python -m jobs.fetch_industry --report-only   # coverage from what is on disk
    python -m jobs.fetch_industry --limit 20      # fetch at most 20 new symbols

Symbols: every distinct `ticker` in the v1 universe on any decision date
(data/backtest/v1_corrected/ranks.parquet) plus every EQ-series symbol traded
in the last 30 sessions of data/daily. Output: data/reference/industry_nse.parquet.

The labels are TODAY's classification applied to the past -- see
zen/data/industry.py.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys

import pandas as pd

from zen.data import industry

RANKS = "data/backtest/v1_corrected/ranks.parquet"
HOLDINGS = "data/backtest/v1_corrected/holdings.csv"


def recent_eq_symbols(sessions: int = 30) -> tuple[set[str], str, str]:
    files = sorted(glob.glob("data/daily/*/*.parquet"))
    frames, n_dates = [], 0
    for f in reversed(files):            # read back only as far as needed
        d = pd.read_parquet(f, columns=["date", "symbol", "series"])
        frames.append(d)
        n_dates = pd.concat(frames)["date"].nunique()
        if n_dates >= sessions:
            break
    d = pd.concat(frames)
    dates = sorted(d["date"].unique())[-sessions:]
    eq = d[d["date"].isin(dates) & (d["series"] == "EQ")]
    return set(eq["symbol"]), str(dates[0])[:10], str(dates[-1])[:10]


def symbol_list() -> list[str]:
    r = pd.read_parquet(RANKS, columns=["ticker"])
    tickers = set(r["ticker"].dropna())
    eq, lo, hi = recent_eq_symbols()
    print(f"v1 tickers {len(tickers):,} | EQ symbols {lo}..{hi} {len(eq):,} | "
          f"union {len(tickers | eq):,}")
    return sorted(tickers | eq)


def report(symbols: list[str]) -> None:
    df = industry.load()
    want = set(symbols)
    got = df[df["symbol"].isin(want)]
    final = got[got["note"].map(industry.is_final)]
    lab = got[got["note"] == "ok"]
    cur = lab[lab["label_scheme"] == "current"]
    print(f"\nFETCH STATUS: {len(final):,} of {len(want):,} requested symbols final "
          f"({len(want) - len(final):,} not final: "
          f"{(got['note'].str.startswith('error')).sum():,} transient errors, "
          f"{len(want - set(got['symbol'])):,} never tried)")
    print(f"  labelled {len(lab):,} (current scheme {len(cur):,}, legacy {len(lab) - len(cur):,}) "
          f"| unavailable {got['note'].str.startswith('unavailable').sum():,}")
    print("  http_status:", got["http_status"].value_counts(dropna=False).to_dict())

    sector_of = lab.set_index("symbol")["sector"]
    cur_of = cur.set_index("symbol")["sector"]

    # v1 company ids: labelled if the ticker it traded under on ANY decision
    # date has a label; the latest date's ticker wins when several do.
    r = pd.read_parquet(RANKS, columns=["symbol", "ticker", "D"]).sort_values("D")
    pairs = r.drop_duplicates(["symbol", "ticker"], keep="last")
    def id_cov(m):
        hit = pairs[pairs["ticker"].isin(m.index)]
        return hit["symbol"].nunique()
    n_ids = r["symbol"].nunique()
    print(f"\nv1 COMPANY IDS: {n_ids:,} | with any NSE label {id_cov(sector_of):,} "
          f"| with a current-scheme label {id_cov(cur_of):,}")

    h = pd.read_csv(HOLDINGS)
    blank = h["sector"].isna() | (h["sector"].astype(str).str.strip() == "")
    hb = h[blank]
    print(f"\nV1 HELD SLOTS (holdings.csv rows): {len(h):,} | unlabelled before {len(hb):,} "
          f"({len(hb) / len(h):.1%}) across {hb['symbol'].nunique():,} company ids")
    for name, m in (("any label", sector_of), ("current-scheme label", cur_of)):
        now = hb["ticker"].isin(m.index)
        print(f"  now with {name}: {now.sum():,} of {len(hb):,} rows "
              f"({hb.loc[now, 'symbol'].nunique():,} of {hb['symbol'].nunique():,} ids); "
              f"still blank {(~now).sum():,}")
    still = hb[~hb["ticker"].isin(cur_of.index)]
    if len(still):
        print("  still without a current label (ticker: rows):",
              still["ticker"].value_counts().head(25).to_dict())

    print("\nTOP 10 SECTORS, current scheme, all fetched symbols:")
    print(cur["sector"].value_counts().head(10).to_string())
    ids_latest = pairs.drop_duplicates("symbol", keep="last")
    v1 = ids_latest.merge(cur[["symbol", "sector"]].rename(columns={"symbol": "ticker"}),
                          on="ticker")
    print("\nTOP 10 SECTORS, current scheme, v1 company ids (latest ticker):")
    print(v1["sector"].value_counts().head(10).to_string())
    print(f"\ndistinct current-scheme values: macro {cur['macro'].nunique()}, "
          f"sector {cur['sector'].nunique()}, industry {cur['industry'].nunique()}, "
          f"basic_industry {cur['basic_industry'].nunique()}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--report-only", action="store_true")
    p.add_argument("--limit", type=int, help="fetch at most this many new symbols")
    args = p.parse_args()

    symbols = symbol_list()
    if not args.report_only:
        todo = symbols
        if args.limit is not None:
            df = industry.load()
            have = set(df.loc[df["note"].map(industry.is_final), "symbol"]) if len(df) else set()
            todo = [s for s in symbols if s not in have][: args.limit]
        try:
            industry.fetch_all(todo)
        except industry.Blocked as e:
            print(f"\nSTOPPED: {e}. NSE is refusing the request; progress is saved "
                  f"and a rerun resumes. Not working around it.")
            report(symbols)
            return 2
    report(symbols)
    return 0


if __name__ == "__main__":
    sys.exit(main())

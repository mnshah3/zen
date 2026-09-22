"""Approximate total-return (TRI) series for the benchmark indices.

    python -m zen.data.index_tri          # rebuild data/indices/derived/tri.parquet

WHY THIS IS DERIVED RATHER THAN DOWNLOADED

NSE's official Total Returns Index history is published only on
niftyindices.com (the "Historical Data - Total Returns Index" page and the
Backpage.aspx endpoint behind it). That site's Terms of Use forbid "any
systematic or automated data collection activities (including scraping, data
mining, data extraction and data harvesting)" without written consent, and its
disclaimer forbids storing its content in a retrieval system. So the official
TRI is not fetched by code here. NSE's own daily archive file (ind_close_all,
already used for the price levels) carries no TRI row for any broad index --
its only "TR" rows are the leveraged and inverse Nifty 50 products -- but it
does carry each index's dividend yield every day, and that is enough to
reconstruct the dividend leg approximately.

If the official series is wanted, a person can download it by hand from
niftyindices.com for personal, non-commercial use; nothing here depends on it.

METHOD

For each index, on consecutive stored sessions t-1, t:

    TRI_t = TRI_{t-1} * ( close_t / close_{t-1}  +  y_{t-1} / 100 / 252 )

    close  NSE's published price-index close (data/indices, price-only)
    y      NSE's published dividend yield in percent for the index at the
           PREVIOUS close, i.e. the yield known before session t opened
    252    sessions per year: each session accrues 1/252 of a year's yield

TRI starts equal to the price close on the index's first session with a
published yield, so tri/close - 1 is the cumulative dividend contribution.
Dividends are reinvested (the accrual compounds), and are gross of tax, which
matches NSE's gross TRI rather than its net-of-tax NTR variant. A yield NSE
leaves blank is carried forward for up to MAX_FILL sessions and treated as zero
beyond that; `yield_filled` marks those rows. The series starts after the
last gap of more than MAX_GAP_DAYS calendar days in the stored levels, so an
isolated back-calculated row years before an index's launch is not bridged.

WHAT MAKES IT APPROXIMATE

  * Timing. NSE reinvests each dividend on its ex-date. Here the year's
    dividends are spread evenly across sessions. Indian dividends bunch in
    July to September (after AGMs), so over a quarter the derived series can
    lead or lag the official one by a few tenths of a percent. Over whole
    years the error largely cancels.
  * Definition. NSE's yield is trailing-twelve-month dividends over current
    market value. Accruing it forward assumes next year's dividends match last
    year's.
  * Rounding. The yield is printed to two decimals (0.98), worth at most about
    0.005 percentage points a year.
  * Session count. A year has roughly 245-250 NSE sessions, not 252, so the
    accrual delivers about 97-99% of the stated yield, roughly 0.02 points a
    year short at a 1.2% yield. A session missing from the archive loses one
    day's accrual (~0.005%).

Use it for what the backtest spec needs -- "Nifty 500 with dividends" -- and
say it is approximate wherever it is reported. For a price-only comparison use
`close`, which is exact.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from zen.data.indices import PARQUET_DIR

log = logging.getLogger(__name__)

OUT = PARQUET_DIR / "derived" / "tri.parquet"
SESSIONS_PER_YEAR = 252
MAX_FILL = 5
MAX_GAP_DAYS = 14

# Broad-market indices the backtest compares against.
DERIVE_FOR = ("Nifty 500", "Nifty 50", "Nifty Midcap 150", "Nifty Smallcap 250",
              "Nifty Total Market", "Nifty MidSmallcap 400", "Nifty Microcap 250",
              "Nifty500 Equal Weight")

COLUMNS = ["date", "index_name", "close", "div_yield", "yield_used",
           "yield_filled", "tri"]


def derive(levels: pd.DataFrame) -> pd.DataFrame:
    """Approximate TRI for ONE index from its (date, close, div_yield) rows."""
    df = (levels[["date", "close", "div_yield"]]
          .dropna(subset=["close"])
          .sort_values("date").reset_index(drop=True))
    # A gap longer than any market closure means the rows before it are not
    # part of the same continuous record (NSE regenerated a few old files with
    # back-calculated values for indices launched years later). Accruing one
    # session of yield across a multi-year hole would be wrong, so start after
    # the last such gap.
    gap = pd.to_datetime(df["date"]).diff().dt.days > MAX_GAP_DAYS
    if gap.any():
        cut = gap[gap].index[-1]
        log.info("%s rows before %s dropped (gap > %d days)",
                 cut, df.loc[cut, "date"], MAX_GAP_DAYS)
        df = df.loc[cut:].reset_index(drop=True)
    first = df["div_yield"].first_valid_index()
    if first is None:
        return pd.DataFrame(columns=COLUMNS)
    df = df.loc[first:].reset_index(drop=True)

    filled = df["div_yield"].ffill(limit=MAX_FILL)
    df["yield_filled"] = df["div_yield"].isna() & filled.notna()
    df["yield_used"] = filled.fillna(0.0)

    gross = df["close"] / df["close"].shift(1) \
        + df["yield_used"].shift(1) / 100.0 / SESSIONS_PER_YEAR
    gross.iloc[0] = 1.0
    df["tri"] = df["close"].iloc[0] * np.cumprod(gross.to_numpy())
    return df


def build(src: Path = PARQUET_DIR, names=DERIVE_FOR) -> pd.DataFrame:
    con = duckdb.connect()
    raw = con.execute(
        f"SELECT date, index_name, close, div_yield "
        f"FROM read_parquet('{src.as_posix()}/*.parquet', union_by_name=true) "
        f"WHERE index_name IN ({', '.join('?' * len(names))}) "
        f"ORDER BY index_name, date", list(names)).df()
    con.close()
    out = []
    for name, g in raw.groupby("index_name"):
        t = derive(g)
        t["index_name"] = name
        out.append(t)
    missing = set(names) - set(raw["index_name"])
    if missing:
        log.warning("no stored levels for %s", ", ".join(sorted(missing)))
    if not out:
        return pd.DataFrame(columns=COLUMNS)
    res = pd.concat(out, ignore_index=True).reindex(columns=COLUMNS)
    res["date"] = pd.to_datetime(res["date"]).dt.date
    return res


def write(df: pd.DataFrame, out: Path = OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False, compression="zstd")
    return out


def load(name: str | None = None, path: Path = OUT) -> pd.DataFrame:
    df = pd.read_parquet(path)
    return df[df["index_name"] == name].reset_index(drop=True) if name else df


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df = build()
    if df.empty:
        log.error("no index levels to derive from")
        return 1
    p = write(df)
    cov = df.groupby("index_name")["date"].agg(["min", "max", "count"])
    print(f"wrote {len(df):,} rows to {p}")
    print(cov.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

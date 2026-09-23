"""Backfill `prices_other` (every series except EQ/BE) from the raw bhavcopy zips.

Spec Clarification 34. A stock NSE moves to the trade-for-trade segment (BZ and
similar) keeps trading but disappears from `prices`, so the 20-session no-trade
exit sold it as if it had stopped. `update_prices` has kept the non-EQ/BE rows
since the split was introduced, but nothing ever went back over the history.
This does, from the zips already in data/raw -- no network.

WHAT IT MUST NOT DO

It never writes to data/daily. The EQ/BE archive (`prices`) is the universe the
v1 results were computed on and must stay byte-for-byte what it is. Each file
is parsed with the project's own parser (`bhavcopy._normalise`, the same call
`fetch_day` makes), split with `bhavcopy.split_series`, and ONLY the non-EQ/BE
half is written, with store's own writer, to data/daily_other (one parquet per
month, key date, symbol, series). The EQ/BE half is only counted, so the log can
show the parse agrees with the stored archive.

RESUMABLE

Work goes a month at a time and each month is written in one call, so an
interruption costs at most the month in progress. On restart, a month is
skipped when every raw date in it is already present in its parquet. A date
whose file has no non-EQ/BE rows at all can never appear there and would be
re-parsed each run; that is harmless (the write is idempotent on the key) and
the summary counts such dates.

NOTHING SKIPPED SILENTLY

A file that cannot be opened, parses to nothing (the parser returns an empty
frame when the file's internal date disagrees with its name), or yields rows
with a null symbol or series is listed in the summary, and the job exits 1.

    python -m jobs.backfill_prices_other
    python -m jobs.backfill_prices_other --start 2022-10-01 --end 2022-12-31
    python -m jobs.backfill_prices_other --redo      # ignore what is stored
"""

from __future__ import annotations

import argparse
import logging
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from zen.data import bhavcopy, store

log = logging.getLogger(__name__)

RAW_DIR = Path("data/raw")
KEY = ("date", "symbol", "series")


def raw_files(raw_dir: Path, start: date | None, end: date | None) -> dict[str, list[tuple[date, Path]]]:
    """Raw zips grouped by month, 'YYYY-MM' -> [(session date, path)], sorted."""
    by_month: dict[str, list[tuple[date, Path]]] = defaultdict(list)
    for p in sorted(raw_dir.glob("*.zip")):
        d = datetime.strptime(p.stem, "%Y%m%d").date()
        if (start and d < start) or (end and d > end):
            continue
        by_month[f"{d:%Y-%m}"].append((d, p))
    return dict(sorted(by_month.items()))


def parse_file(d: date, p: Path) -> pd.DataFrame:
    """One raw zip -> normalised frame, as `bhavcopy.fetch_day` does it, with ONE
    deliberate difference in how the CSV is read.

    `fetch_day` calls `pd.read_csv` with pandas' default NA tokens, which turn
    the literal text "NA" into a missing value. NSE uses "NA" as a real series
    code in the legacy files (bonds such as IRFC and NHAI print as series NA),
    so those rows came out with a null series -- which `prices_other` declares
    NOT NULL, so a rebuild would reject the whole table. Here only an EMPTY
    field counts as missing. Numeric columns are unaffected (`_normalise`
    coerces them, and any non-number becomes NaN either way); the only rows
    that change are string fields holding an NA-like word. That the EQ/BE half
    of every file still matches data/daily exactly is checked separately.
    """
    with zipfile.ZipFile(p) as z:
        name = z.namelist()[0]
        df = pd.read_csv(z.open(name), low_memory=False,
                         keep_default_na=False, na_values=[""])
    if df.empty:
        raise ValueError("raw file has no rows")
    out = bhavcopy._normalise(df, d)
    if out.empty:
        raise ValueError("parser returned no rows (internal date mismatch or no STK/EQ instruments)")
    return out


def stored_dates(month: str, out_dir: Path) -> set[date]:
    p = out_dir / month[:4] / f"{month}.parquet"
    if not p.exists():
        return set()
    # An unreadable file here is a write interrupted mid-flight. Stop rather
    # than guess: deleting it could lose rows that did not come from data/raw.
    stored = pd.read_parquet(p, columns=["date"])
    return set(pd.to_datetime(stored["date"]).dt.date)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--raw", type=Path, default=RAW_DIR)
    ap.add_argument("--out", type=Path, default=store.PARQUET_DIR_OTHER)
    ap.add_argument("--redo", action="store_true", help="re-parse months already stored")
    args = ap.parse_args()

    months = raw_files(args.raw, args.start, args.end)
    n_files = sum(len(v) for v in months.values())
    log.info("%d raw files in %d months", n_files, len(months))

    failed: list[tuple[date, str]] = []
    no_other: list[date] = []
    dupes: list[tuple[date, int]] = []
    per_year: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    parsed = skipped_months = written_rows = 0

    for month, files in months.items():
        if not args.redo:
            have = stored_dates(month, args.out)
            if all(d in have for d, _ in files):
                skipped_months += 1
                continue

        frames = []
        for d, p in files:
            try:
                df = parse_file(d, p)
            except Exception as e:                                   # noqa: BLE001
                failed.append((d, f"{type(e).__name__}: {e}"))
                log.error("%s: FAILED %s: %s", p.name, type(e).__name__, e)
                continue
            eq, rest = bhavcopy.split_series(df)
            parsed += 1
            yr = f"{d:%Y}"
            per_year[yr]["files"] += 1
            per_year[yr]["eq_be_rows"] += len(eq)
            per_year[yr]["other_rows"] += len(rest)
            if rest.empty:
                no_other.append(d)
                continue
            bad = rest["symbol"].isna() | rest["series"].isna()
            if bad.any():
                # prices_other declares both NOT NULL; a rebuild would reject
                # the whole table over these, so the file is failed, not written.
                failed.append((d, f"{int(bad.sum())} rows with null symbol/series"))
                log.error("%s: FAILED %d rows with null symbol/series", p.name, int(bad.sum()))
                continue
            n_dup = int(rest.duplicated(subset=list(KEY)).sum())
            if n_dup:
                dupes.append((d, n_dup))
                log.warning("%s: %d duplicate (date, symbol, series) rows; writer keeps last",
                            p.name, n_dup)
            frames.append(rest)

        if frames:
            month_df = pd.concat(frames, ignore_index=True)
            store.write_parquet(month_df, args.out, KEY)
            written_rows += len(month_df)
            log.info("%s: %d files, %d other-series rows written", month, len(files), len(month_df))

    print("\n=== backfill_prices_other summary ===")
    print(f"raw files considered: {n_files} | months skipped as already stored: {skipped_months}")
    print(f"files parsed this run: {parsed} | other-series rows written this run: {written_rows:,}")
    if per_year:
        print("per year (this run):  files  eq_be_rows  other_rows")
        for yr in sorted(per_year):
            r = per_year[yr]
            print(f"  {yr}             {r['files']:5d}  {r['eq_be_rows']:10,}  {r['other_rows']:10,}")
    print(f"dates with no non-EQ/BE rows: {len(no_other)} {[str(x) for x in no_other[:20]]}")
    print(f"dates with duplicate keys: {len(dupes)} {[(str(a), b) for a, b in dupes[:20]]}")
    print(f"FAILED files: {len(failed)}")
    for d, why in failed:
        print(f"  {d:%Y%m%d}.zip  {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

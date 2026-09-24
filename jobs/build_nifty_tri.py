"""Build data/external/nifty_tri.parquet from the niftyindices.com CSVs.

    python -m jobs.build_nifty_tri            # build, validate, write
    python -m jobs.build_nifty_tri --check    # validate only, write nothing

Input: every CSV in data/external/niftyindices/, exported from niftyindices.com,
Reports, Historical Data, "Total returns Index Values", one year or less per
export (see data/external/SOURCES.md). Two layouts exist:

    "IndexName","Date","Total Returns Index"
    "IndexName","Date","Total Returns Index","Net Total Return(s) Index"

Output columns: index_name, date, tri (gross), net_tri (NaN where NSE gives no
net series or prints "-").

The build refuses to write if:
  * the same (index, date) appears in two files with different values;
  * a value is missing, non-numeric or not positive;
  * an index skips a session that NIFTY 500 traded, or has a session NIFTY 500
    did not, anywhere inside the index's own date range;
  * an index has a calendar gap of more than 7 days;
  * an index that was already in the parquet loses or changes any row.
Daily moves over 15% are listed but do not fail the build (4 Jun 2024 is real).
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path("data/external/niftyindices")
OUT = Path("data/external/nifty_tri.parquet")
CALENDAR = "NIFTY 500"
MAX_GAP_DAYS = 7
BIG_MOVE = 0.15


def read_one(path: str) -> pd.DataFrame:
    d = pd.read_csv(path, dtype=str, keep_default_na=False)
    d.columns = [c.strip() for c in d.columns]
    need = ["IndexName", "Date", "Total Returns Index"]
    if d.columns[:3].tolist() != need or len(d.columns) not in (3, 4):
        raise ValueError(f"{path}: unexpected header {d.columns.tolist()}")
    if len(d.columns) == 4 and d.columns[3] not in ("Net Total Returns Index", "Net Total Return Index"):
        raise ValueError(f"{path}: unexpected fourth column {d.columns[3]!r}")
    out = pd.DataFrame({
        "index_name": d["IndexName"].str.strip(),
        "date": pd.to_datetime(d["Date"].str.strip(), format="%d %b %Y"),
        "tri": pd.to_numeric(d["Total Returns Index"].str.strip(), errors="coerce"),
    })
    net = d.iloc[:, 3].str.strip() if len(d.columns) == 4 else None
    out["net_tri"] = (pd.to_numeric(net.replace("-", ""), errors="coerce")
                      if net is not None else np.nan)
    bad = out["tri"].isna() | ~(out["tri"] > 0)
    if bad.any():
        raise ValueError(f"{path}: {int(bad.sum())} rows with a missing or non-positive TRI")
    out["file"] = Path(path).name
    return out


def build() -> tuple[pd.DataFrame, list[str], list[str]]:
    files = sorted(glob.glob(str(SRC / "*.csv")))
    if not files:
        raise SystemExit(f"no CSVs in {SRC}")
    raw = pd.concat([read_one(f) for f in files], ignore_index=True)
    errors, notes = [], []

    # the same session in two exports must carry the same numbers
    key = ["index_name", "date"]
    g = raw.groupby(key).agg(n=("tri", "size"), tri_n=("tri", "nunique"),
                             net_n=("net_tri", lambda s: s.dropna().nunique()))
    clash = g[(g["tri_n"] > 1) | (g["net_n"] > 1)]
    for (name, d), _ in clash.iterrows():
        rows = raw[(raw.index_name == name) & (raw.date == d)]
        errors.append(f"{name} {d.date()}: files disagree: "
                      + "; ".join(f"{r.file}={r.tri}" for r in rows.itertuples()))
    notes.append(f"{len(files)} files, {len(raw)} rows, "
                 f"{int((g['n'] > 1).sum())} sessions exported twice (all identical)"
                 if clash.empty else f"{len(clash)} sessions disagree")

    # net_tri: keep a value if any export has one
    t = (raw.sort_values(key + ["net_tri"], na_position="last")
            .drop_duplicates(key, keep="first")
            .drop(columns="file")
            .sort_values(key).reset_index(drop=True))

    cal = set(t.loc[t.index_name == CALENDAR, "date"])
    if not cal:
        errors.append(f"{CALENDAR} missing; it is the session calendar")
    for name, s in t.groupby("index_name"):
        dates = s["date"]
        lo, hi = dates.min(), dates.max()
        gap = dates.diff().dt.days.max()
        if gap > MAX_GAP_DAYS:
            at = dates[dates.diff().dt.days == gap].iloc[0]
            errors.append(f"{name}: {gap}-day gap ending {at.date()}")
        if name != CALENDAR and cal:
            inside = {d for d in cal if lo <= d <= min(hi, max(cal))}
            have = set(dates[dates <= max(cal)])
            miss, extra = sorted(inside - have), sorted(have - inside)
            if miss:
                errors.append(f"{name}: missing {len(miss)} {CALENDAR} sessions, "
                              f"e.g. {[str(d.date()) for d in miss[:5]]}")
            if extra:
                errors.append(f"{name}: {len(extra)} sessions {CALENDAR} does not have, "
                              f"e.g. {[str(d.date()) for d in extra[:5]]}")
        r = s.set_index("date")["tri"].pct_change().abs()
        for d, v in r[r > BIG_MOVE].items():
            notes.append(f"{name} {d.date()}: daily move {v:.1%} (listed, not an error)")
        notes.append(f"{name}: {len(s)} sessions {lo.date()} -> {hi.date()}, max gap {int(gap)}d, "
                     f"net_tri {'yes' if s['net_tri'].notna().any() else 'no'}")

    # the indices already in use must not change
    if OUT.exists():
        old = pd.read_parquet(OUT)
        old["date"] = pd.to_datetime(old["date"]).astype("datetime64[ns]")
        for name, o in old.groupby("index_name"):
            n = t[t.index_name == name].set_index("date")
            o = o.set_index("date")
            lost = o.index.difference(n.index)
            if len(lost):
                errors.append(f"{name}: {len(lost)} rows in the current parquet are gone")
            both = o.index.intersection(n.index)
            dt = (o.loc[both, "tri"] != n.loc[both, "tri"]).sum()
            dn = (~((o.loc[both, "net_tri"] == n.loc[both, "net_tri"])
                    | (o.loc[both, "net_tri"].isna() & n.loc[both, "net_tri"].isna()))).sum()
            if dt or dn:
                errors.append(f"{name}: {dt} tri and {dn} net_tri values differ from the current parquet")
            else:
                notes.append(f"{name}: all {len(both)} existing rows unchanged, "
                             f"{len(n) - len(both)} added")
    t["date"] = t["date"].astype("datetime64[us]")
    return t[["index_name", "date", "tri", "net_tri"]], errors, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="validate only")
    a = ap.parse_args(argv)
    t, errors, notes = build()
    for n in notes:
        print(n)
    if errors:
        print("\nREFUSED:")
        for e in errors:
            print("  " + e)
        return 1
    if a.check:
        print("\ncheck passed; nothing written")
        return 0
    tmp = OUT.with_suffix(".tmp.parquet")
    t.to_parquet(tmp, index=False)
    tmp.replace(OUT)
    print(f"\nwrote {OUT}: {len(t)} rows, {t.index_name.nunique()} indices")
    return 0


if __name__ == "__main__":
    sys.exit(main())

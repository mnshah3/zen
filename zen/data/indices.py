"""NSE index levels -- the real benchmarks.

NSE publishes a daily close file covering every index it computes, in the same
archive that serves the bhavcopy. That gives Nifty 50, Nifty 500, Nifty Total
Market, and the midcap and smallcap series directly, rather than approximating
them from constituent prices.

Which benchmark to use depends on what the strategy is allowed to buy. A
multicap screen picking from roughly two thousand names should be measured
against a broad index, not the Nifty 50: beating fifty large caps while holding
small caps mostly measures the size effect rather than the selection. Nifty 500
and Nifty Total Market are the honest hurdles; Nifty 50 is useful context.

These are the published series, which carry the index provider's own survivorship
characteristics -- membership is revised, and dropped constituents leave. That is
fine for a benchmark, because it is what an investor could actually have bought.
It is NOT fine for building a universe, which is why the strategy's own universe
still comes from the bhavcopy archive.

THE LEVELS ARE PRICE-ONLY. ind_close_all carries no total-return rows for the
broad indices (its only "TR" rows are the leveraged/inverse Nifty 50 products),
so dividends are missing from `close`. The file does carry the index P/E, P/B
and dividend yield, which are stored here, and `zen.data.index_tri` uses the
dividend yield to derive an approximate total-return series.

NAMES DRIFT, SO MATCHING IS BY CANONICAL NAME, NOT BY STRING EQUALITY

Until 2026-09-21 the filter was an exact, case-sensitive match against KEEP.
That silently dropped:

  * everything before 2015-11-09, when NSE renamed the CNX family
    ("CNX Nifty" -> "Nifty 50", "CNX 500" -> "Nifty 500", ...);
  * Nifty Midcap 150, Smallcap 250, Microcap 250 and MidSmallcap 400 on every
    day, because KEEP spelt them "NIFTY Midcap 150" while NSE writes
    "Nifty Midcap 150";
  * the midcap/smallcap 100 series in the years NSE spelt them differently.

Names are now normalised (whitespace collapsed, case folded) and mapped through
ALIASES to one canonical spelling -- NSE's current one -- so a series keeps a
single name across NSE's renames. Every alias below was verified by level
continuity on the switch date: the new name's close minus its points change
equals the old name's previous close.

Column units: open/high/low/close/points_change in index points, pct_change and
div_yield in percent (1.21 means 1.21%), pe and pb as ratios, volume in shares,
turnover in Rs crore. NSE prints "-" where a field does not apply; that is NULL.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
PARQUET_DIR = Path("data/indices")

# Pause between requests to NSE's archive. One file per session is all a daily
# run needs; a backfill of ten years is ~3,000 requests, which at this pace is
# under an hour and never looks like a burst.
REQUEST_DELAY_S = 1.0

# The series worth storing, in NSE's current spelling (checked against the
# 2026-09-18 file). Everything else NSE computes is sectoral or
# strategy-flavoured and would just bloat the table.
KEEP = {
    "Nifty 50", "Nifty Next 50", "Nifty 100", "Nifty 200", "Nifty 500",
    "Nifty Total Market", "NIFTY Midcap 100", "Nifty Midcap 150",
    "NIFTY Smallcap 100", "Nifty Smallcap 250", "Nifty Microcap 250",
    "Nifty MidSmallcap 400", "Nifty500 Equal Weight",
    "Nifty Bank", "Nifty IT", "Nifty Auto",
    "Nifty Pharma", "Nifty FMCG", "Nifty Metal", "Nifty Realty",
    "Nifty Energy", "Nifty Infrastructure", "Nifty PSU Bank",
}

# Former spellings -> canonical name. Keys are normalised (see _norm). Each
# was verified by level continuity on the day the name changed.
ALIASES = {
    # 2015-11-09: NSE retired the CNX brand.
    "cnx nifty": "Nifty 50",
    "cnx nifty junior": "Nifty Next 50",
    "cnx 100": "Nifty 100",
    "cnx 200": "Nifty 200",
    "cnx 500": "Nifty 500",
    "cnx midcap": "NIFTY Midcap 100",
    "cnx smallcap": "NIFTY Smallcap 100",
    "cnx auto": "Nifty Auto",
    "cnx bank": "Nifty Bank",
    "cnx energy": "Nifty Energy",
    "cnx fmcg": "Nifty FMCG",
    "cnx it": "Nifty IT",
    "cnx metal": "Nifty Metal",
    "cnx pharma": "Nifty Pharma",
    "cnx psu bank": "Nifty PSU Bank",
    "cnx realty": "Nifty Realty",
    "cnx infrastructure": "Nifty Infrastructure",
    # 2016-04-01: the midcap and smallcap 100 moved to free-float weighting
    # and were published under these names until NSE dropped the prefix. The
    # separate "Nifty Full Midcap/Smallcap 100" rows are a different series
    # (they do not chain) and are deliberately not mapped.
    "nifty free float midcap 100": "NIFTY Midcap 100",
    "nifty free float smallcap 100": "NIFTY Smallcap 100",
}

COLUMNS = ["date", "index_name", "open", "high", "low", "close",
           "points_change", "pct_change", "volume", "turnover",
           "pe", "pb", "div_yield"]
NUMERIC = COLUMNS[2:]

# Column order matters only for SELECT *; every insert below names its columns.
# New columns go at the END so an existing table can be migrated with ALTER.
SCHEMA = """
CREATE TABLE IF NOT EXISTS indices (
    date          DATE    NOT NULL,
    index_name    VARCHAR NOT NULL,
    open          DOUBLE,
    high          DOUBLE,
    low           DOUBLE,
    close         DOUBLE,
    points_change DOUBLE,
    pct_change    DOUBLE,
    volume        DOUBLE,
    turnover      DOUBLE,
    pe            DOUBLE,
    pb            DOUBLE,
    div_yield     DOUBLE,
    PRIMARY KEY (date, index_name)
);
"""

_RENAME = {
    "Index Name": "index_name", "Open Index Value": "open",
    "High Index Value": "high", "Low Index Value": "low",
    "Closing Index Value": "close", "Points Change": "points_change",
    "Change(%)": "pct_change", "Volume": "volume",
    "Turnover (Rs. Cr.)": "turnover", "P/E": "pe", "P/B": "pb",
    "Div Yield": "div_yield",
}


def _norm(name) -> str:
    return " ".join(str(name).split()).casefold()


_LOOKUP = {_norm(k): k for k in KEEP} | {_norm(k): v for k, v in ALIASES.items()}


def canonical_name(name) -> str | None:
    """The stored name for an NSE index label, or None if it is not kept.

    Case- and whitespace-insensitive, and follows NSE's renames, so
    canonical_name("CNX 500") and canonical_name("NIFTY 500") are both
    "Nifty 500".
    """
    return _LOOKUP.get(_norm(name))


def _num(s):
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce")


def parse_csv(text: str, d: date) -> pd.DataFrame | None:
    """Parse one ind_close_all file into COLUMNS, kept indices only.

    The date we requested is authoritative; the date inside the file is
    unreliable across years, exactly as with the bhavcopy.
    """
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as e:                                       # noqa: BLE001
        log.warning("%s: unparseable index csv (%s)", d, e)
        return None

    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns=_RENAME)
    if "index_name" not in df or "close" not in df:
        return None

    raw = df["index_name"].astype(str).str.strip()
    df["index_name"] = raw.map(canonical_name)
    # A row whose label IS the canonical spelling beats one reached through an
    # alias, should NSE ever print both on one day.
    df["_exact"] = raw.map(_norm) == df["index_name"].map(
        lambda n: _norm(n) if isinstance(n, str) else None)
    df = df[df["index_name"].notna()].copy()
    if df.empty:
        return None
    df = (df.sort_values("_exact", ascending=False)
            .drop_duplicates(subset="index_name", keep="first"))

    df["date"] = d
    for c in NUMERIC:
        df[c] = _num(df[c]).astype("float64") if c in df else float("nan")
    return (df.reindex(columns=COLUMNS)
              .sort_values("index_name").reset_index(drop=True))


def fetch_text(d: date, session=None) -> str | None:
    """One session's raw file, or None if there is none (holiday, not yet out)."""
    s = session or _session()
    try:
        r = s.get(URL.format(d=d), timeout=30)
    except Exception as e:                                       # noqa: BLE001
        log.warning("%s: index fetch failed (%s)", d, e)
        return None
    if r.status_code == 404 or len(r.content) < 200:
        return None
    if r.status_code != 200:
        log.warning("%s: index file returned %s", d, r.status_code)
        return None
    return r.text


def fetch_day(d: date, session=None) -> pd.DataFrame | None:
    """One session's index closes, or None if the market was shut."""
    text = fetch_text(d, session=session)
    return parse_csv(text, d) if text is not None else None


def fetch_range(start: date, end: date, session=None) -> pd.DataFrame:
    s = session or _session()
    frames, d = [], start
    while d <= end:
        if d.weekday() < 5:
            df = fetch_day(d, session=s)
            if df is not None and not df.empty:
                frames.append(df)
            time.sleep(REQUEST_DELAY_S)
        d += timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def ensure_schema(con) -> None:
    """Create the table, or add columns a database built before them lacks.

    A local DuckDB file keeps whatever shape it was created with, and
    CREATE TABLE IF NOT EXISTS will not change it. Without this, an existing
    database would reject the new columns while CI (which builds from
    nothing) accepted them -- the exact local/CI split test_schema_parity
    exists to catch.
    """
    con.execute(SCHEMA)
    have = {r[0] for r in con.execute("DESCRIBE indices").fetchall()}
    for c in COLUMNS:
        if c not in have:
            con.execute(f"ALTER TABLE indices ADD COLUMN {c} DOUBLE")


_COLS_SQL = ", ".join(COLUMNS)


def upsert(con, df: pd.DataFrame, replace: bool = False) -> int:
    """Insert rows; with replace=True a re-fetched day overwrites the stored one."""
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_idx", df.reindex(columns=COLUMNS))
    before = con.execute("SELECT count(*) FROM indices").fetchone()[0]
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    con.execute(f"{verb} INTO indices ({_COLS_SQL}) "
                f"SELECT {_COLS_SQL} FROM incoming_idx")
    after = con.execute("SELECT count(*) FROM indices").fetchone()[0]
    con.unregister("incoming_idx")
    return after - before


def stored_dates(con) -> set:
    ensure_schema(con)
    return {r[0] for r in con.execute("SELECT DISTINCT date FROM indices").fetchall()}


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    """One parquet per calendar year, merged with what is already there.

    Only YYYY.parquet belongs at the top of out_dir: rebuild_from_parquet and
    the parity test read it with a bare *.parquet glob. Derived series live in
    the derived/ subdirectory, which that glob does not descend into.
    """
    if df.empty:
        return []
    written = []
    for year, chunk in df.groupby(df["date"].map(lambda d: f"{d:%Y}")):
        p = out_dir / f"{year}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = (chunk.reindex(columns=COLUMNS)
                      .drop_duplicates(subset=["date", "index_name"], keep="last")
                      .sort_values(["date", "index_name"]))
        for c in NUMERIC:
            chunk[c] = chunk[c].astype("float64")
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con, out_dir: Path = PARQUET_DIR) -> int:
    ensure_schema(con)
    if not out_dir.exists():
        return 0
    con.execute("DELETE FROM indices")
    con.execute(
        f"INSERT INTO indices ({_COLS_SQL}) SELECT {_COLS_SQL} FROM "
        f"read_parquet('{out_dir}/*.parquet', union_by_name=true)"
    )
    return con.execute("SELECT count(*) FROM indices").fetchone()[0]


def series(con, name: str) -> pd.DataFrame:
    """Price-only closes for one index; any NSE spelling of the name works."""
    ensure_schema(con)
    return con.execute(
        "SELECT date, close FROM indices WHERE index_name = ? ORDER BY date",
        [canonical_name(name) or name]).df()


def forward_return(con, name: str, start: date, months: int) -> float | None:
    """Index return over `months` from `start`, or None if it runs past the data."""
    s = series(con, name)
    if s.empty:
        return None
    s["date"] = pd.to_datetime(s["date"])
    at = s[s["date"] >= pd.Timestamp(start)]
    if at.empty:
        return None
    row0 = at.iloc[0]
    later = s[s["date"] >= row0["date"] + pd.DateOffset(months=months)]
    if later.empty:
        return None
    return float(later.iloc[0]["close"] / row0["close"] - 1)

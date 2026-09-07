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
"""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
PARQUET_DIR = Path("data/indices")

# The series worth storing. Everything else NSE computes is sectoral or
# strategy-flavoured and would just bloat the table.
KEEP = {
    "Nifty 50", "Nifty Next 50", "Nifty 100", "Nifty 200", "Nifty 500",
    "Nifty Total Market", "NIFTY Midcap 100", "NIFTY Midcap 150",
    "NIFTY Smallcap 100", "NIFTY Smallcap 250", "NIFTY Microcap 250",
    "Nifty Midsmallcap 400", "Nifty Bank", "Nifty IT", "Nifty Auto",
    "Nifty Pharma", "Nifty FMCG", "Nifty Metal", "Nifty Realty",
    "Nifty Energy", "Nifty Infrastructure", "Nifty PSU Bank",
}

COLUMNS = ["date", "index_name", "open", "high", "low", "close",
           "points_change", "pct_change", "volume", "turnover"]

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
    PRIMARY KEY (date, index_name)
);
"""


def _num(s):
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce")


def fetch_day(d: date, session=None) -> pd.DataFrame | None:
    """One session's index closes, or None if the market was shut."""
    s = session or _session()
    try:
        r = s.get(URL.format(d=d), timeout=30)
    except Exception as e:
        log.warning("%s: index fetch failed (%s)", d, e)
        return None
    if r.status_code == 404 or len(r.content) < 200:
        return None
    if r.status_code != 200:
        log.warning("%s: index file returned %s", d, r.status_code)
        return None

    try:
        df = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        log.warning("%s: unparseable index csv (%s)", d, e)
        return None

    df.columns = [c.strip() for c in df.columns]
    rename = {
        "Index Name": "index_name", "Open Index Value": "open",
        "High Index Value": "high", "Low Index Value": "low",
        "Closing Index Value": "close", "Points Change": "points_change",
        "Change(%)": "pct_change", "Volume": "volume",
        "Turnover (Rs. Cr.)": "turnover",
    }
    df = df.rename(columns=rename)
    if "index_name" not in df or "close" not in df:
        return None

    df["index_name"] = df["index_name"].astype(str).str.strip()
    df = df[df["index_name"].isin(KEEP)].copy()
    if df.empty:
        return None

    # The date column inside the file is unreliable across years; the date we
    # requested is authoritative, exactly as with the bhavcopy.
    df["date"] = d
    for c in ["open", "high", "low", "close", "points_change",
              "pct_change", "volume", "turnover"]:
        df[c] = _num(df[c]) if c in df else None
    return df.reindex(columns=COLUMNS).reset_index(drop=True)


def fetch_range(start: date, end: date, session=None) -> pd.DataFrame:
    s = session or _session()
    frames, d = [], start
    while d <= end:
        if d.weekday() < 5:
            df = fetch_day(d, session=s)
            if df is not None and not df.empty:
                frames.append(df)
        d += timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def upsert(con, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_idx", df)
    before = con.execute("SELECT count(*) FROM indices").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO indices SELECT * FROM incoming_idx")
    after = con.execute("SELECT count(*) FROM indices").fetchone()[0]
    con.unregister("incoming_idx")
    return after - before


def stored_dates(con) -> set:
    ensure_schema(con)
    return {r[0] for r in con.execute("SELECT DISTINCT date FROM indices").fetchall()}


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    if df.empty:
        return []
    written = []
    for year, chunk in df.groupby(df["date"].map(lambda d: f"{d:%Y}")):
        p = out_dir / f"{year}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = (chunk.drop_duplicates(subset=["date", "index_name"], keep="last")
                      .sort_values(["date", "index_name"]))
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con, out_dir: Path = PARQUET_DIR) -> int:
    ensure_schema(con)
    if not out_dir.exists():
        return 0
    con.execute("DELETE FROM indices")
    con.execute(
        f"INSERT INTO indices SELECT * FROM "
        f"read_parquet('{out_dir}/*.parquet', union_by_name=true)"
    )
    return con.execute("SELECT count(*) FROM indices").fetchone()[0]


def series(con, name: str) -> pd.DataFrame:
    ensure_schema(con)
    return con.execute(
        "SELECT date, close FROM indices WHERE index_name = ? ORDER BY date",
        [name]).df()


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

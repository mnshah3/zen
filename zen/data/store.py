"""DuckDB-backed store for the daily price archive.

One file, no server, fast enough for a few million rows -- and it runs
unchanged inside GitHub Actions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = Path("data/zen.duckdb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    date        DATE     NOT NULL,
    symbol      VARCHAR  NOT NULL,
    series      VARCHAR,
    isin_code   VARCHAR,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    prev_close  DOUBLE,
    volume      BIGINT,
    turnover    DOUBLE,
    trades      BIGINT,
    PRIMARY KEY (date, symbol)
);
"""


# Everything NSE printed that is NOT series EQ or BE (spec Clarification 34).
# Same columns, separate table: `prices` must stay bit-for-bit what it was, or
# the pre-registered universe moves. The primary key carries `series`, because
# one symbol can print in two non-equity series on one session.
SCHEMA_OTHER = """
CREATE TABLE IF NOT EXISTS prices_other (
    date        DATE     NOT NULL,
    symbol      VARCHAR  NOT NULL,
    series      VARCHAR  NOT NULL,
    isin_code   VARCHAR,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    prev_close  DOUBLE,
    volume      BIGINT,
    turnover    DOUBLE,
    trades      BIGINT,
    PRIMARY KEY (date, symbol, series)
);
"""


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    con.execute(SCHEMA_OTHER)
    return con


def upsert(con: duckdb.DuckDBPyConnection, df: pd.DataFrame,
           table: str = "prices") -> int:
    """Insert rows, ignoring days already stored. Safe to re-run."""
    if df.empty:
        return 0
    con.register("incoming", df)
    before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    con.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM incoming")
    after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    con.unregister("incoming")
    return after - before


def stored_dates(con: duckdb.DuckDBPyConnection) -> set[date]:
    rows = con.execute("SELECT DISTINCT date FROM prices").fetchall()
    return {r[0] for r in rows}


def coverage(con: duckdb.DuckDBPyConnection) -> dict:
    row = con.execute(
        "SELECT min(date), max(date), count(DISTINCT date), "
        "count(DISTINCT symbol), count(*) FROM prices"
    ).fetchone()
    return {"first": row[0], "last": row[1], "trading_days": row[2],
            "symbols": row[3], "rows": row[4]}


PARQUET_DIR = Path("data/daily")
# Deliberately NOT under data/daily: several jobs and the schema-parity test
# glob data/daily/**/*.parquet as "the price archive", and these rows must
# never reach it (Clarification 34).
PARQUET_DIR_OTHER = Path("data/daily_other")


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR,
                  key: tuple[str, ...] = ("date", "symbol")) -> list[Path]:
    """One parquet per calendar month.

    Monthly rather than daily: ~130 files for a decade instead of ~2,500, and
    columnar compression works far better over a month of rows than over a
    single session. A day appended to an existing month rewrites that one file,
    which is cheap.
    """
    written = []
    months = df["date"].map(lambda d: f"{d:%Y-%m}")
    for month, chunk in df.groupby(months):
        p = out_dir / month[:4] / f"{month}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        # Belt and braces: this is where freshly fetched rows meet stored ones,
        # so it is the one place a dtype mismatch between them can surface. A
        # mixed date/Timestamp column sorts fine on pandas 2 and raises on
        # pandas 3, which is a failure worth making impossible rather than
        # merely fixing upstream.
        chunk["date"] = pd.to_datetime(chunk["date"])
        chunk = (chunk.drop_duplicates(subset=list(key), keep="last")
                      .sort_values(list(key)))
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con: duckdb.DuckDBPyConnection,
                         out_dir: Path = PARQUET_DIR,
                         table: str = "prices") -> int:
    """Recreate the whole archive from committed parquet files."""
    pattern = str(out_dir / "**" / "*.parquet")
    if not list(Path(out_dir).glob("**/*.parquet")):
        return 0
    con.execute(f"DELETE FROM {table}")
    con.execute(
        f"INSERT INTO {table} SELECT * FROM read_parquet('{pattern}', union_by_name=true)"
    )
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def rebuild_other_from_parquet(con: duckdb.DuckDBPyConnection,
                               out_dir: Path = PARQUET_DIR_OTHER) -> int:
    """Recreate `prices_other` (the non-EQ/BE series) from parquet."""
    return rebuild_from_parquet(con, out_dir, "prices_other")

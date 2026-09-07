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


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def upsert(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """Insert rows, ignoring days already stored. Safe to re-run."""
    if df.empty:
        return 0
    con.register("incoming", df)
    before = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO prices SELECT * FROM incoming")
    after = con.execute("SELECT count(*) FROM prices").fetchone()[0]
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


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    """One small parquet per trading day -- git-friendly and incremental."""
    written = []
    for d, chunk in df.groupby("date"):
        p = out_dir / f"{d:%Y}" / f"{d:%Y-%m-%d}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con: duckdb.DuckDBPyConnection,
                         out_dir: Path = PARQUET_DIR) -> int:
    """Recreate the whole archive from committed parquet files."""
    pattern = str(out_dir / "**" / "*.parquet")
    con.execute("DELETE FROM prices")
    con.execute(
        f"INSERT INTO prices SELECT * FROM read_parquet('{pattern}', union_by_name=true)"
    )
    return con.execute("SELECT count(*) FROM prices").fetchone()[0]

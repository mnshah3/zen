"""Shareholding patterns and promoter pledges, from NSE.

Two tables, both point-in-time: every row carries the moment NSE broadcast it, so a reader
asking "what was known on date D" filters on broadcast_dt <= D and takes the latest filing
for each period. Revisions arrive as new filings (new record_id) and are kept, never merged
over the original.

  shareholding   one row per filing: promoter and promoter-group %, public %, employee
                 trusts %, for the quarter ending period_end
                 source: /api/corporate-share-holdings-master (date-range query)
  pledges        NSE's pledge summary per company. The feed's own timestamp is its refresh
                 time (the same for every company), so a row is kept only when a company's
                 figures change, stamped observed_dt: when zen first saw that state
                 source: /api/corporate-pledgedata (all companies, latest disclosure)

Like the other tables, the committed parquet under data/shareholding and data/pledges is the
archive; jobs.rebuild_db reloads the DuckDB tables from it (as optional tables: a missing or
broken file warns and never stops the rebuild).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

SH_API = ("https://www.nseindia.com/api/corporate-share-holdings-master"
          "?index=equities&from_date={start}&to_date={end}")
PL_API = "https://www.nseindia.com/api/corporate-pledgedata?index=equities"

SH_DIR = Path("data/shareholding")
PL_DIR = Path("data/pledges")

SH_COLUMNS = ["symbol", "isin", "company", "period_end", "promoter_pct", "public_pct", "employee_trusts_pct",
              "broadcast_dt", "submission_date", "revised", "record_id", "xbrl_url"]
PL_COLUMNS = ["symbol", "company", "shp_date", "observed_dt", "promoter_holding_pct",
              "promoter_pledged_pct", "pledged_pct_of_total", "shares_pledged", "promoter_shares",
              "issued_shares"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS shareholding (
    symbol              VARCHAR NOT NULL,
    isin                VARCHAR,
    company             VARCHAR,
    period_end          DATE    NOT NULL,
    promoter_pct        DOUBLE,
    public_pct          DOUBLE,
    employee_trusts_pct DOUBLE,
    broadcast_dt        TIMESTAMP,
    submission_date     DATE,
    revised             VARCHAR,
    record_id           VARCHAR NOT NULL,
    xbrl_url            VARCHAR,
    PRIMARY KEY (record_id)
);
CREATE TABLE IF NOT EXISTS pledges (
    symbol                VARCHAR NOT NULL,
    company               VARCHAR,
    shp_date              DATE,
    observed_dt           TIMESTAMP NOT NULL,
    promoter_holding_pct  DOUBLE,
    promoter_pledged_pct  DOUBLE,
    pledged_pct_of_total  DOUBLE,
    shares_pledged        DOUBLE,
    promoter_shares       DOUBLE,
    issued_shares         DOUBLE,
    PRIMARY KEY (symbol, observed_dt)
);
"""


def _num(v) -> float | None:
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s not in ("", "-", "None", "null", "NA") else None
    except (TypeError, ValueError):
        return None


def _dt(v, fmts=("%d-%b-%Y %H:%M:%S", "%d-%b-%Y")) -> datetime | None:
    s = str(v or "").strip()
    for f in fmts:
        try:
            return datetime.strptime(s.title() if "%b" in f else s, f)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- shareholding

def parse_shareholding(rows: list) -> pd.DataFrame:
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("symbol") or not r.get("recordId"):
            continue
        pe = _dt(r.get("date"), ("%d-%b-%Y",))
        if pe is None:
            continue
        sub = _dt(r.get("submissionDate"), ("%d-%b-%Y",))
        out.append({"symbol": str(r["symbol"]).strip(), "isin": r.get("isin"), "company": r.get("name"),
                    "period_end": pe.date(), "promoter_pct": _num(r.get("pr_and_prgrp")),
                    "public_pct": _num(r.get("public_val")), "employee_trusts_pct": _num(r.get("employeeTrusts")),
                    "broadcast_dt": _dt(r.get("broadcastDate")), "submission_date": sub.date() if sub else None,
                    "revised": r.get("revisedData"), "record_id": str(r["recordId"]), "xbrl_url": r.get("xbrl")})
    return pd.DataFrame(out, columns=SH_COLUMNS)


def fetch_shareholding(start: date, end: date, session=None, chunk_days: int = 30) -> pd.DataFrame:
    """Every shareholding filing broadcast between start and end, in chunks NSE answers quickly."""
    s = session or _session()
    frames, d = [], start
    while d <= end:
        e = min(end, d + timedelta(days=chunk_days - 1))
        url = SH_API.format(start=f"{d:%d-%m-%Y}", end=f"{e:%d-%m-%Y}")
        try:
            r = s.get(url, timeout=60)
            r.raise_for_status()
            frames.append(parse_shareholding(r.json()))
        except Exception as ex:                                      # noqa: BLE001
            log.warning("shareholding %s to %s failed (%s)", d, e, ex)
        d = e + timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=SH_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=["record_id"], keep="last").reset_index(drop=True)


# --------------------------------------------------------------------------- pledges

def parse_pledges(payload) -> pd.DataFrame:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = (r.get("symbol") or r.get("comSymbol") or "").strip()
        b = _dt(r.get("broadcastDt"))
        if not b:
            continue
        shp = _dt(r.get("shp"), ("%d-%b-%Y",))
        out.append({"symbol": sym or None, "company": (r.get("comName") or "").strip(),
                    "shp_date": shp.date() if shp else None, "observed_dt": b,
                    "promoter_holding_pct": _num(r.get("percPromoterHolding")),
                    "promoter_pledged_pct": _num(r.get("percPromoterShares")),
                    "pledged_pct_of_total": _num(r.get("percSharesPledged")),
                    "shares_pledged": _num(r.get("numSharesPledged")),
                    "promoter_shares": _num(r.get("totPromoterHolding")),
                    "issued_shares": _num(r.get("totIssuedShares"))})
    return pd.DataFrame(out, columns=PL_COLUMNS)


def fetch_pledges(session=None) -> pd.DataFrame:
    s = session or _session()
    r = s.get(PL_API, timeout=60)
    r.raise_for_status()
    df = parse_pledges(r.json())
    return df


def attach_symbols(df: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    """The bulk pledge feed names companies but not always symbols: map by exact company name
    from the NSE equity list, and drop what does not map rather than guess."""
    if df.empty:
        return df
    by_name = {v.strip().lower(): k for k, v in names.items()}
    df = df.copy()
    miss = df["symbol"].isna() | (df["symbol"] == "")
    df.loc[miss, "symbol"] = df.loc[miss, "company"].str.strip().str.lower().map(by_name)
    return df[df["symbol"].notna() & (df["symbol"] != "")].reset_index(drop=True)


# --------------------------------------------------------------------------- storage

def ensure_schema(con) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)


def _upsert(con, table: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_sh", df)
    before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    con.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM incoming_sh")
    after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    con.unregister("incoming_sh")
    return after - before


def upsert_shareholding(con, df: pd.DataFrame) -> int:
    return _upsert(con, "shareholding", df[SH_COLUMNS] if not df.empty else df)


STATE_COLS = ["shp_date", "promoter_holding_pct", "promoter_pledged_pct", "pledged_pct_of_total",
              "shares_pledged"]


def changed_pledges(con, df: pd.DataFrame) -> pd.DataFrame:
    """The rows of a snapshot whose figures differ from the latest stored state of that company."""
    if df.empty:
        return df
    ensure_schema(con)
    last = con.execute(
        f"""SELECT symbol, {", ".join(STATE_COLS)} FROM (
                SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY observed_dt DESC) AS rn FROM pledges
            ) WHERE rn = 1""").df()
    if last.empty:
        return df.reset_index(drop=True)

    def key(frame):
        k = frame[["symbol"] + STATE_COLS].copy()
        k["shp_date"] = pd.to_datetime(k["shp_date"]).dt.strftime("%Y-%m-%d")
        for c in STATE_COLS[1:]:
            k[c] = pd.to_numeric(k[c]).round(4)
        return k.apply(lambda r: "|".join("" if pd.isna(v) else str(v) for v in r), axis=1)

    seen = set(key(last))
    return df[~key(df).isin(seen)].reset_index(drop=True)


def upsert_pledges(con, df: pd.DataFrame) -> int:
    return _upsert(con, "pledges", df[PL_COLUMNS] if not df.empty else df)


def _write(df: pd.DataFrame, out_dir: Path, year_col: str, key: list[str]) -> list[Path]:
    if df.empty:
        return []
    written = []
    years = pd.to_datetime(df[year_col]).dt.year
    for year, chunk in df.groupby(years):
        p = out_dir / f"{int(year)}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = chunk.drop_duplicates(subset=key, keep="last").sort_values(key)
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def write_shareholding(df: pd.DataFrame, out_dir: Path = SH_DIR) -> list[Path]:
    return _write(df[SH_COLUMNS] if not df.empty else df, out_dir, "period_end", ["record_id"])


def write_pledges(df: pd.DataFrame, out_dir: Path = PL_DIR) -> list[Path]:
    return _write(df[PL_COLUMNS] if not df.empty else df, out_dir, "observed_dt", ["symbol", "observed_dt"])


def _rebuild(con, table: str, out_dir: Path) -> int:
    ensure_schema(con)
    if not out_dir.exists() or not any(out_dir.glob("*.parquet")):
        return 0
    con.execute(f"DELETE FROM {table}")
    con.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM read_parquet('{out_dir}/*.parquet', union_by_name=true)")
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def rebuild_shareholding(con, out_dir: Path = SH_DIR) -> int:
    return _rebuild(con, "shareholding", out_dir)


def rebuild_pledges(con, out_dir: Path = PL_DIR) -> int:
    return _rebuild(con, "pledges", out_dir)


# --------------------------------------------------------------------------- point-in-time reads

def asof(con, symbol: str, when: date, periods: int = 8) -> pd.DataFrame:
    """The last `periods` quarters of shareholding as known at the end of `when`: for each
    period, the latest filing broadcast on or before that day. Read-only: an archive without
    the table gives an empty frame."""
    if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'shareholding'"
                       ).fetchone()[0]:
        return pd.DataFrame(columns=["period_end", "promoter_pct", "public_pct", "employee_trusts_pct",
                                     "broadcast_dt"])
    return con.execute(
        """
        SELECT period_end, promoter_pct, public_pct, employee_trusts_pct, broadcast_dt
        FROM (
            SELECT *, row_number() OVER (PARTITION BY period_end ORDER BY broadcast_dt DESC, record_id DESC) AS rn
            FROM shareholding
            WHERE symbol = ? AND broadcast_dt < CAST(? AS DATE) + INTERVAL 1 DAY
        ) WHERE rn = 1
        ORDER BY period_end DESC
        LIMIT ?
        """, [symbol, when, periods]).df()

"""Market-wide daily data from NSE that the price file does not carry: how much of each stock's
volume was delivered, the day's bulk and block deals, foreign and domestic institutions' net
buying, and the stocks under exchange surveillance. Research data for the dashboard; no strategy
reads it.

SOURCES, AS MEASURED ON 19 OCT 2026

  delivery      nsearchives /archives/equities/mto/MTO_DDMMYYYY.DAT, "Security Wise Delivery
                Position", one file a session from 2015 (the fuller sec_bhavdata_full file only
                from 2020). Quantity traded matches zen's prices: HAVELLS on 16 Oct 2026 traded
                723,675 shares in both, 329,647 delivered (45.55%).
  deals         nsearchives /content/equities/bulk.csv and block.csv: the latest session's deals
                only. NSE's history endpoint answers a script with a blocked page, so the record
                starts the day zen first reads it and grows by a session each day.
  fii_dii       www.nseindia.com /api/fiidiiTradeReact: the latest session's provisional cash
                market figures, Rs crore. Same: no history, a record from the first read on.
  surveillance  www.nseindia.com /api/reportASM (long and short term) and /api/reportGSM: the
                lists as they stand. Each read is kept with the time it was seen, so the record
                says which stocks were under which measure on each day zen looked.

Equity series only (EQ, BE) for delivery, the two the universe is built from. The quantity traded
in an MTO file equalled zen's volume in all 16,129 rows of 9-16 October 2026, so the files keep only
the quantity delivered (sorted and compressed, about a third of the size) and the table takes the
volume and the percentage from zen's prices when it is rebuilt.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

ARCHIVE = "https://nsearchives.nseindia.com"
MTO_URL = ARCHIVE + "/archives/equities/mto/MTO_{d:%d%m%Y}.DAT"
DEAL_URLS = {"bulk": ARCHIVE + "/content/equities/bulk.csv", "block": ARCHIVE + "/content/equities/block.csv"}
FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
ASM_URL = "https://www.nseindia.com/api/reportASM"
GSM_URL = "https://www.nseindia.com/api/reportGSM"
SERIES = ("EQ", "BE")

DELIVERY_DIR = Path("data/delivery")
DEALS_DIR = Path("data/deals")
FLOWS_DIR = Path("data/flows")
SURV_DIR = Path("data/surveillance")

DELIVERY_COLUMNS = ["date", "symbol", "series", "traded_qty", "deliv_qty", "deliv_pct"]
DELIVERY_STORED = ["date", "symbol", "series", "deliv_qty"]
DEAL_COLUMNS = ["date", "kind", "symbol", "security", "client", "side", "qty", "price", "remarks", "observed_dt"]
FLOW_COLUMNS = ["date", "category", "buy_cr", "sell_cr", "net_cr", "observed_dt"]
SURV_COLUMNS = ["observed_dt", "list", "symbol", "company", "isin", "stage", "code", "description"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery (date DATE, symbol VARCHAR, series VARCHAR, traded_qty BIGINT,
    deliv_qty BIGINT, deliv_pct DOUBLE);
CREATE TABLE IF NOT EXISTS deals (date DATE, kind VARCHAR, symbol VARCHAR, security VARCHAR, client VARCHAR,
    side VARCHAR, qty BIGINT, price DOUBLE, remarks VARCHAR, observed_dt TIMESTAMP);
CREATE TABLE IF NOT EXISTS fii_dii (date DATE, category VARCHAR, buy_cr DOUBLE, sell_cr DOUBLE, net_cr DOUBLE,
    observed_dt TIMESTAMP);
CREATE TABLE IF NOT EXISTS surveillance (observed_dt TIMESTAMP, list VARCHAR, symbol VARCHAR, company VARCHAR,
    isin VARCHAR, stage VARCHAR, code VARCHAR, description VARCHAR);
"""


# ---------------------------------------------------------------- delivery
def parse_mto(text: str) -> pd.DataFrame:
    """The records of an MTO file: `20,<sr>,<symbol>,<series>,<traded>,<delivered>,<pct>`, with the
    trade date from its header line `Trade Date <16-OCT-2026>`."""
    m = re.search(r"Trade Date <(\d{2}-[A-Z]{3}-\d{4})>", text)
    if not m:
        raise ValueError("no trade date in the MTO header")
    day = datetime.strptime(m.group(1).title(), "%d-%b-%Y").date()
    rows = []
    for line in text.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 7 or p[0] != "20" or p[3] not in SERIES:
            continue
        try:
            traded, deliv, pct = int(p[4]), int(p[5]), float(p[6])
        except ValueError:
            continue
        rows.append((day, p[2], p[3], traded, deliv, pct))
    return pd.DataFrame(rows, columns=DELIVERY_COLUMNS)


def fetch_delivery(session, d: date) -> pd.DataFrame | None:
    """One session's delivery position, or None where NSE has no file (a holiday)."""
    r = session.get(MTO_URL.format(d=d), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    df = parse_mto(r.text)
    if not df.empty and df["date"].iloc[0] != d:
        raise ValueError(f"MTO file for {d} carries trade date {df['date'].iloc[0]}")
    return df


def write_delivery(df: pd.DataFrame, out_dir: Path | None = None) -> list[Path]:
    """Merge into one file a month (YYYY/YYYY-MM.parquet), a session replacing its earlier copy."""
    out_dir = out_dir or DELIVERY_DIR                  # read at call time, so a test can point it elsewhere
    written = []
    if df.empty:
        return written
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    for month, chunk in df.groupby(df["date"].dt.to_period("M")):
        path = out_dir / str(month.year) / f"{month}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = pd.read_parquet(path)
            old = old[~old["date"].isin(chunk["date"].unique())]
            chunk = pd.concat([old, chunk], ignore_index=True)
        chunk = chunk.sort_values(["symbol", "series", "date"])[DELIVERY_STORED]
        chunk.to_parquet(path, index=False, compression="zstd", compression_level=9)
        written.append(path)
    return written


def stored_delivery_days(out_dir: Path | None = None) -> set:
    out_dir = out_dir or DELIVERY_DIR
    files = sorted(out_dir.glob("*/*.parquet")) if out_dir.exists() else []
    days = set()
    for f in files:
        days |= set(pd.to_datetime(pd.read_parquet(f, columns=["date"])["date"]).dt.date)
    return days


# ---------------------------------------------------------------- deals
def parse_deals(text: str, kind: str, observed: datetime) -> pd.DataFrame:
    """bulk.csv or block.csv: Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded,
    Trade Price / Wght. Avg. Price[, Remarks]. A file reading "NO RECORDS" is an empty session."""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Symbol"].notna() & (df["Date"].str.strip().str.upper() != "NO RECORDS")]
    if df.empty:
        return pd.DataFrame(columns=DEAL_COLUMNS)
    num = lambda s: pd.to_numeric(s.str.replace(",", "", regex=False).str.strip(), errors="coerce")   # noqa: E731
    out = pd.DataFrame({
        "date": pd.to_datetime(df["Date"].str.strip(), format="%d-%b-%Y").dt.date,
        "kind": kind, "symbol": df["Symbol"].str.strip(), "security": df["Security Name"].str.strip(),
        "client": df["Client Name"].str.strip(), "side": df["Buy/Sell"].str.strip().str.upper(),
        "qty": num(df["Quantity Traded"]).astype("Int64"),
        "price": num(df["Trade Price / Wght. Avg. Price"]),
        "remarks": df["Remarks"].str.strip() if "Remarks" in df else None,
        "observed_dt": pd.Timestamp(observed)})
    out.loc[out["remarks"].isin(["-", ""]), "remarks"] = None
    return out[DEAL_COLUMNS]


def write_yearly(df: pd.DataFrame, out_dir: Path, key: list[str], when: str, columns: list[str]) -> list[Path]:
    """Merge into one file a year by the `when` column's year, a row with the same `key` replacing
    its earlier copy (a file read twice in a session is stored once)."""
    written = []
    if df.empty:
        return written
    df = df.copy()
    year = pd.to_datetime(df[when]).dt.year
    for y, chunk in df.groupby(year):
        path = out_dir / f"{int(y)}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = pd.read_parquet(path)
            chunk = pd.concat([old, chunk], ignore_index=True)
        chunk = chunk.drop_duplicates(subset=key, keep="last")[columns]
        chunk.to_parquet(path, index=False)
        written.append(path)
    return written


# ---------------------------------------------------------------- institutional flows
def parse_fii_dii(data: list, observed: datetime) -> pd.DataFrame:
    """[{"category": "DII" | "FII/FPI", "date": "16-Oct-2026", "buyValue", "sellValue", "netValue"}],
    values in Rs crore as NSE states them."""
    rows = []
    for x in data or []:
        rows.append({"date": datetime.strptime(x["date"].strip(), "%d-%b-%Y").date(), "category": x["category"].strip(),
                     "buy_cr": float(str(x["buyValue"]).replace(",", "")), "sell_cr": float(str(x["sellValue"]).replace(",", "")),
                     "net_cr": float(str(x["netValue"]).replace(",", "")), "observed_dt": pd.Timestamp(observed)})
    return pd.DataFrame(rows, columns=FLOW_COLUMNS)


# ---------------------------------------------------------------- surveillance
def parse_asm(data: dict, observed: datetime) -> pd.DataFrame:
    rows = []
    for key, name in (("longterm", "ASM long term"), ("shortterm", "ASM short term")):
        for x in ((data or {}).get(key) or {}).get("data") or []:
            rows.append({"observed_dt": pd.Timestamp(observed), "list": name, "symbol": x.get("symbol"), "company": x.get("companyName"),
                         "isin": x.get("isin"), "stage": x.get("asmSurvIndicator"), "code": x.get("survCode"), "description": x.get("survDesc")})
    return pd.DataFrame(rows, columns=SURV_COLUMNS)


def parse_gsm(data: list, observed: datetime) -> pd.DataFrame:
    rows = [{"observed_dt": pd.Timestamp(observed), "list": "GSM", "symbol": x.get("symbol"), "company": x.get("companyName"),
             "isin": x.get("isin"), "stage": x.get("gsmStage"), "code": x.get("survCode"), "description": x.get("survDesc")}
            for x in data or []]
    return pd.DataFrame(rows, columns=SURV_COLUMNS)


# ---------------------------------------------------------------- the archive
def ensure_schema(con) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)


def rebuild(con) -> int:
    """Recreate the four tables from the committed parquet; the row count across them."""
    ensure_schema(con)
    con.execute("DELETE FROM delivery")
    if DELIVERY_DIR.exists() and any(DELIVERY_DIR.glob("*/*.parquet")):
        # the volume and the percentage from zen's prices (equal to NSE's own, see above)
        con.execute(f"""INSERT INTO delivery
            SELECT d.date, d.symbol, d.series, p.volume, d.deliv_qty,
                   CASE WHEN p.volume > 0 THEN round(100.0 * d.deliv_qty / p.volume, 2) END
            FROM read_parquet('{(DELIVERY_DIR / "*/*.parquet").as_posix()}', union_by_name=true) d
            LEFT JOIN prices p ON p.date = d.date AND p.symbol = d.symbol AND p.series = d.series""")
    total = con.execute("SELECT count(*) FROM delivery").fetchone()[0]
    for table, folder, glob, cols in (("deals", DEALS_DIR, "*.parquet", DEAL_COLUMNS),
                                      ("fii_dii", FLOWS_DIR, "*.parquet", FLOW_COLUMNS),
                                      ("surveillance", SURV_DIR, "*.parquet", SURV_COLUMNS)):
        con.execute(f"DELETE FROM {table}")
        if folder.exists() and any(folder.glob(glob)):
            con.execute(f"INSERT INTO {table} SELECT {', '.join(cols)} "
                        f"FROM read_parquet('{(folder / glob).as_posix()}', union_by_name=true)")
        total += con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return total

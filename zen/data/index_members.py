"""The constituents of NSE's main indices, as NSE lists them (26 Oct 2026).

SOURCE, AS MEASURED ON 26 OCT 2026
  https://nsearchives.nseindia.com/content/indices/ind_<name>list.csv, one small CSV per index
  (Nifty 50 is ind_nifty50list.csv, 3.3 KB): Company Name, Industry, Symbol, Series, ISIN Code. NSE
  answers an ordinary browser's request. The file carries no date and no weights: NSE publishes
  weights only in its monthly factsheets.

KEPT: each index's list as zen read it, stored again only when it changes, with observed_dt the
time zen first read that version (IST). A list read on day D is what NSE showed on D, so a backtest
sees a membership only from when zen did. Weights are not stored: the markets page works an
approximate weight out of each company's market value less its promoters' holding, the free-float
idea NSE's own weights rest on, and says so.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/content/indices/{file}.csv"
# zen's index names (zen.data.indices) and NSE's list files
LISTS = {
    "Nifty 50": "ind_nifty50list", "Nifty Next 50": "ind_niftynext50list", "Nifty 100": "ind_nifty100list",
    "Nifty 200": "ind_nifty200list", "Nifty 500": "ind_nifty500list", "Nifty Midcap 150": "ind_niftymidcap150list",
    "Nifty Smallcap 250": "ind_niftysmallcap250list", "Nifty Bank": "ind_niftybanklist", "Nifty IT": "ind_niftyitlist",
    "Nifty Pharma": "ind_niftypharmalist", "Nifty Auto": "ind_niftyautolist", "Nifty FMCG": "ind_niftyfmcglist",
    "Nifty Metal": "ind_niftymetallist", "Nifty Energy": "ind_niftyenergylist", "Nifty Realty": "ind_niftyrealtylist",
    "Nifty PSU Bank": "ind_niftypsubanklist",
}
DIR = Path("data/index_members")
FILE = "index_members.parquet"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/141.0 Safari/537.36", "Accept": "text/csv,*/*;q=0.8"}
COLUMNS = ["index_name", "symbol", "company", "industry", "series", "isin", "observed_dt"]
SCHEMA = """CREATE TABLE IF NOT EXISTS index_members (index_name VARCHAR, symbol VARCHAR, company VARCHAR,
    industry VARCHAR, series VARCHAR, isin VARCHAR, observed_dt TIMESTAMP);"""


def parse(text: str, index_name: str, observed: datetime) -> pd.DataFrame:
    """One list file into rows. Raises on a file that is not a constituents list."""
    df = pd.read_csv(io.StringIO(text.lstrip("﻿")), dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"company name", "industry", "symbol", "series", "isin code"}
    if not need <= set(df.columns):
        raise ValueError(f"{index_name}: unexpected columns {list(df.columns)}")
    df = df.rename(columns={"company name": "company", "isin code": "isin"})
    df = df[df["symbol"].notna()].copy()
    for c in ("company", "industry", "symbol", "series", "isin"):
        df[c] = df[c].astype(str).str.strip()
    if df.empty:
        raise ValueError(f"{index_name}: no constituents")
    df["index_name"] = index_name
    df["observed_dt"] = pd.Timestamp(observed)
    return df[COLUMNS].sort_values("symbol").reset_index(drop=True)


def stored(out_dir: Path | None = None) -> pd.DataFrame:
    p = (out_dir or DIR) / FILE
    return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=COLUMNS)


def latest(df: pd.DataFrame) -> dict[str, set]:
    """The last stored list of each index, as sets of symbols."""
    if df.empty:
        return {}
    last = df.groupby("index_name")["observed_dt"].transform("max")
    cur = df[df["observed_dt"] == last]
    return {k: set(g["symbol"]) for k, g in cur.groupby("index_name")}


def write(new: list[pd.DataFrame], out_dir: Path | None = None) -> int:
    """Store each list that differs from the last one stored for its index. Returns lists stored."""
    d = out_dir or DIR
    old = stored(d)
    have = latest(old)
    keep = [x for x in new if set(x["symbol"]) != have.get(x["index_name"].iloc[0])]
    if not keep:
        return 0
    d.mkdir(parents=True, exist_ok=True)
    allr = pd.concat([old, *keep], ignore_index=True) if len(old) else pd.concat(keep, ignore_index=True)
    allr["observed_dt"] = pd.to_datetime(allr["observed_dt"])
    allr.sort_values(["index_name", "observed_dt", "symbol"])[COLUMNS].to_parquet(d / FILE, index=False)
    return len(keep)


def update(s=None, out_dir: Path | None = None, now: datetime | None = None, pause: float = 0.5) -> int:
    """Read every list; store the changed ones. A list that fails is skipped, the rest go on."""
    import time

    import requests
    s = s or requests.Session()
    s.headers.update(HEADERS)
    now = (now or datetime.now()).replace(microsecond=0)
    frames, failed = [], []
    for name, file in LISTS.items():
        try:
            r = s.get(URL.format(file=file), timeout=30)
            r.raise_for_status()
            frames.append(parse(r.text, name, now))
        except Exception as e:                                   # noqa: BLE001 - one list never stops the rest
            failed.append(name)
            log.warning("index_members: %s: %s", name, e)
        time.sleep(pause)
    n = write(frames, out_dir)
    log.info("index_members: %d lists read, %d changed, failed %s", len(frames), n, failed or "none")
    if failed and not frames:
        raise RuntimeError("no index list could be read")
    return n


def rebuild(con) -> int:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.execute("DELETE FROM index_members")
    p = DIR / FILE
    if p.exists():
        con.execute(f"INSERT INTO index_members SELECT {', '.join(COLUMNS)} FROM read_parquet('{p.as_posix()}')")
    return con.execute("SELECT count(*) FROM index_members").fetchone()[0]

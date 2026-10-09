"""Corporate events NSE lists as coming up: board meetings for results, dividends, fund raising.

Source: NSE's event calendar (https://www.nseindia.com/api/event-calendar?index=equities), the
same list as NSE's website, fetched each evening. Every event is kept with the time it was
first seen (`observed_dt`), so a read "as of D" returns only what was listed before D: a
results date announced on the 9th is not known on the 8th. An event that NSE later drops or
moves stays in the archive with its last sighting (`last_seen_dt`), so a cancelled meeting is
visible as one, not silently rewritten.

Research data for the dashboard and the MCP server: no strategy reads it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

URL = "https://www.nseindia.com/api/event-calendar?index=equities"
WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
OUT = Path("data/events/events.parquet")
COLUMNS = ["symbol", "company", "event_date", "purpose", "description", "is_results",
           "observed_dt", "last_seen_dt"]
KEY = ["symbol", "event_date", "purpose"]
RESULTS = re.compile(r"financial\s+results?", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    symbol VARCHAR, company VARCHAR, event_date DATE, purpose VARCHAR, description VARCHAR,
    is_results BOOLEAN, observed_dt TIMESTAMP, last_seen_dt TIMESTAMP
)
"""


def parse(rows: list, seen: datetime) -> pd.DataFrame:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = (r.get("symbol") or "").strip()
        try:
            d = datetime.strptime((r.get("date") or "").strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        if not sym:
            continue
        purpose = " ".join((r.get("purpose") or "").split())
        desc = " ".join((r.get("bm_desc") or "").split())
        out.append({"symbol": sym, "company": (r.get("company") or "").strip(), "event_date": d,
                    "purpose": purpose, "description": desc,
                    "is_results": bool(RESULTS.search(purpose) or RESULTS.search(desc)),
                    "observed_dt": seen, "last_seen_dt": seen})
    df = pd.DataFrame(out, columns=COLUMNS)
    return df.drop_duplicates(KEY, keep="last")


def fetch(session) -> pd.DataFrame:
    seen = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    try:
        session.get(WARMUP, timeout=25)
    except Exception:                                                # noqa: BLE001
        pass
    r = session.get(URL, timeout=60)
    r.raise_for_status()
    payload = r.json()
    rows = payload if isinstance(payload, list) else (payload.get("data") or [])
    return parse(rows, seen)


def merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Keep the first sighting of every event and update its last sighting and text."""
    if old is None or not len(old):
        return new.reset_index(drop=True)
    old = old.copy()
    for c in ("event_date",):
        old[c] = pd.to_datetime(old[c]).dt.date
    both = old.merge(new[KEY + ["company", "description", "is_results", "last_seen_dt"]], on=KEY, how="outer",
                     suffixes=("", "_new"), indicator=True)
    upd = both["_merge"] != "left_only"
    for c in ("company", "description", "is_results", "last_seen_dt"):
        both.loc[upd, c] = both.loc[upd, c + "_new"]
    both.loc[both["_merge"] == "right_only", "observed_dt"] = both.loc[both["_merge"] == "right_only", "last_seen_dt"]
    return both[COLUMNS].sort_values(["event_date", "symbol"]).reset_index(drop=True)


def load(path: Path = OUT) -> pd.DataFrame:
    return pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)


def write(df: pd.DataFrame, path: Path = OUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="zstd")


def rebuild(con, path: Path = OUT) -> int:
    con.execute("DROP TABLE IF EXISTS events")
    con.execute(SCHEMA)
    if not path.exists():
        return 0
    con.execute(f"INSERT INTO events SELECT {', '.join(COLUMNS)} FROM read_parquet('{path.as_posix()}')")
    return con.execute("SELECT count(*) FROM events").fetchone()[0]


def upcoming(con, asof, days: int = 30, results_only: bool = False, symbol: str | None = None) -> pd.DataFrame:
    """Events dated from `asof` to `asof + days` that were listed before the end of `asof`."""
    tabs = set(con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"])
    if "events" not in tabs:
        return pd.DataFrame(columns=COLUMNS)
    q = ("SELECT * FROM events WHERE event_date >= ? AND event_date <= ? + ? * INTERVAL 1 DAY "
         "AND observed_dt < ? + INTERVAL 1 DAY")
    params = [asof, asof, days, asof]
    if results_only:
        q += " AND is_results"
    if symbol:
        q += " AND symbol = ?"
        params.append(symbol.upper())
    return con.execute(q + " ORDER BY event_date, symbol", params).df()

"""NSE corporate announcements -- what companies actually told the exchange.

This is where a stock's real news lives. A newspaper writes about a capacity
expansion days later, if at all; the filing appears within minutes of the
board approving it. It is also the honest answer to "volume spiked and our
news feeds explain nothing": most of the time a filing does explain it.

Point-in-time integrity matters more here than anywhere else in the archive.
Every row is stamped with `an_dt`, the moment NSE published it, not the moment
we fetched it. A strategy asking what was known on a date must filter on
`an_dt`, or it will "know" about results before they were announced -- exactly
the leak the validation harness exists to catch.

Announcements are joined to prices on `symbol`, which the exchange provides
directly, so no fuzzy name matching is involved.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

API = ("https://www.nseindia.com/api/corporate-announcements"
       "?index=equities&from_date={frm}&to_date={to}")

COLUMNS = ["an_dt", "trade_date", "symbol", "isin", "company", "industry",
           "category", "subject", "url", "has_xbrl"]

# Statutory filings every listed company makes regardless of what is happening
# to it. Two thirds of the feed by volume; matched first so they can never be
# mistaken for news. "Copy of Newspaper Publication" alone was 409 of 1,943
# filings over four sessions.
ROUTINE = re.compile(
    r"copy of newspaper|newspaper (publication|advertisement)|trading window|"
    r"address change|certificate under|compliance certificate|"
    r"reconciliation of share capital|investor complaint|"
    r"corrigendum|change in company secretary|"
    r"regulation 74|regulation 40|depositor(y|ies) and participants",
    re.I)

# Coarse buckets over NSE's free-text description and body. Ordered: the first
# match wins, so the more consequential categories come first.
CATEGORIES: list[tuple[str, str]] = [
    # The exchange formally asking a company to explain an unusual move. Rare,
    # and by construction always attached to a stock the archive has flagged.
    ("volume_query", r"spurt in volume|price (movement|volume)|clarification"
                     r".{0,30}(volume|price)|unusual (movement|volume)"),
    ("results",     r"financial result|quarterly result|audited result|unaudited|"
                    r"earnings release|statement of (profit|accounts)"),
    ("guidance",    r"guidance|outlook|investor (presentation|meet|day)|"
                    r"earnings call|analyst meet|conference call|business update"),
    ("expansion",   r"expansion|capacity|new plant|commission|capex|greenfield|"
                    r"brownfield|acquisition of land|new facility|debottleneck"),
    ("orders",      r"\border\b|contract|letter of (award|intent)|\bloa\b|"
                    r"work order|tender|purchase order|bags? (an )?order"),
    ("mna",         r"amalgamation|merger|demerger|scheme of arrangement|"
                    r"acquisition|divest|stake sale|slump sale|joint venture"),
    ("capital",     r"fund rais|\bqip\b|preferential|allotment|debenture|\bncd\b|"
                    r"issue of (shares|securities)|warrant|rights issue"),
    ("ratings",     r"credit rating|rating action|outlook revis|rating upgrade|"
                    r"rating downgrade"),
    ("litigation",  r"litigation|penalty|show cause|insolvency|\bnclt\b|"
                    r"sebi order|adjudicat|search and seizure|tax demand"),
    ("pledge",      r"pledge|encumbr|promoter (holding|group)|shareholding pattern"),
    ("corp_action", r"dividend|bonus issue|stock split|buyback|record date"),
    ("governance",  r"resignation|appointment|cessation|auditor|director|"
                    r"key managerial|board meeting|shareholders meeting|"
                    r"annual general meeting|\bagm\b|change in management"),
]

# Categories that plausibly move a share price on the day. The rest is stored
# but never surfaced in the brief.
MATERIAL = {"volume_query", "results", "guidance", "expansion", "orders",
            "mna", "capital", "ratings", "litigation"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    an_dt       TIMESTAMP NOT NULL,
    trade_date  DATE      NOT NULL,
    symbol      VARCHAR   NOT NULL,
    isin        VARCHAR,
    company     VARCHAR,
    industry    VARCHAR,
    category    VARCHAR,
    subject     VARCHAR,
    url         VARCHAR,
    has_xbrl    BOOLEAN,
    PRIMARY KEY (an_dt, symbol, subject)
);
"""


def categorise(text: str) -> str:
    """Bucket a filing. Routine compliance is rejected before anything else.

    Order matters: a newspaper advertisement announcing quarterly results is
    still an advertisement, and letting it match "results" would put statutory
    noise in front of the reader every single day.
    """
    low = (text or "").lower()
    if ROUTINE.search(low):
        return "routine"
    for name, pattern in CATEGORIES:
        if re.search(pattern, low):
            return name
    return "other"


def _parse_dt(raw: str) -> datetime | None:
    """NSE stamps look like '07-Sep-2026 20:24:29'."""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def _trade_date(an_dt: datetime) -> date:
    """The session a filing could first affect.

    NSE closes at 15:30. Anything filed after that cannot move the price until
    the next session, so it is attributed forward rather than to the day it
    was published. Getting this wrong is a one-day look-ahead -- the same shape
    of error that produced a fictitious 30% CAGR once already.
    """
    d = an_dt.date()
    if an_dt.hour >= 16 or (an_dt.hour == 15 and an_dt.minute > 30):
        d += timedelta(days=1)
    while d.weekday() >= 5:            # roll Saturday/Sunday to Monday
        d += timedelta(days=1)
    return d


def fetch(start: date, end: date, session=None) -> pd.DataFrame:
    """Announcements published between start and end, inclusive.

    Fetched a day at a time: the endpoint truncates long ranges without
    saying so, and a silently short result here would look like "no news".
    """
    s = session or _session()
    frames, d = [], start

    while d <= end:
        url = API.format(frm=f"{d:%d-%m-%Y}", to=f"{d:%d-%m-%Y}")
        try:
            r = s.get(url, timeout=40)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("%s: announcements failed (%s)", d, e)
            d += timedelta(days=1)
            continue

        rows = payload if isinstance(payload, list) else payload.get("data", [])
        parsed = []
        for row in rows:
            an_dt = _parse_dt(row.get("an_dt") or "")
            symbol = (row.get("symbol") or "").strip()
            if not an_dt or not symbol:
                continue
            subject = re.sub(r"\s+", " ", (row.get("desc") or "")).strip()
            body = re.sub(r"\s+", " ", (row.get("attchmntText") or "")).strip()
            parsed.append({
                "an_dt": an_dt,
                "trade_date": _trade_date(an_dt),
                "symbol": symbol,
                "isin": (row.get("sm_isin") or "").strip() or None,
                "company": (row.get("sm_name") or "").strip() or None,
                "industry": (row.get("smIndustry") or "").strip() or None,
                "category": categorise(f"{subject} {body}"),
                "subject": (f"{subject}: {body}" if body else subject)[:500],
                "url": (row.get("attchmntFile") or "").strip() or None,
                "has_xbrl": bool(row.get("hasXbrl")),
            })

        if parsed:
            frames.append(pd.DataFrame(parsed))
            log.info("%s: %d announcements", d, len(parsed))
        d += timedelta(days=1)

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["an_dt", "symbol", "subject"])[COLUMNS]


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def upsert(con, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_ann", df)
    before = con.execute("SELECT count(*) FROM announcements").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO announcements SELECT * FROM incoming_ann")
    after = con.execute("SELECT count(*) FROM announcements").fetchone()[0]
    con.unregister("incoming_ann")
    return after - before


def for_session(con, trade_date: date, symbols: list[str] | None = None,
                material_only: bool = True) -> pd.DataFrame:
    """Filings that could have moved a given session, optionally for a subset.

    Filtering is on trade_date, which already accounts for after-hours filings
    landing on the next session.
    """
    ensure_schema(con)
    sql = ["SELECT * FROM announcements WHERE trade_date = ?"]
    params: list = [trade_date]

    if material_only:
        placeholders = ", ".join("?" * len(MATERIAL))
        sql.append(f"AND category IN ({placeholders})")
        params.extend(sorted(MATERIAL))
    if symbols:
        placeholders = ", ".join("?" * len(symbols))
        sql.append(f"AND symbol IN ({placeholders})")
        params.extend(symbols)

    sql.append("ORDER BY an_dt DESC")
    return con.execute(" ".join(sql), params).df()


PARQUET_DIR = Path("data/announcements")


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    """One parquet per calendar month, mirroring how prices are stored."""
    if df.empty:
        return []
    written = []
    months = df["trade_date"].map(lambda d: f"{d:%Y-%m}")
    for month, chunk in df.groupby(months):
        p = out_dir / month[:4] / f"{month}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = (chunk.drop_duplicates(subset=["an_dt", "symbol", "subject"])
                      .sort_values(["an_dt", "symbol"]))
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con, out_dir: Path = PARQUET_DIR) -> int:
    """Recreate the announcement table from committed parquet."""
    ensure_schema(con)
    if not out_dir.exists():
        return 0
    pattern = str(out_dir / "**" / "*.parquet")
    con.execute("DELETE FROM announcements")
    con.execute(
        f"INSERT INTO announcements SELECT * FROM "
        f"read_parquet('{pattern}', union_by_name=true)"
    )
    return con.execute("SELECT count(*) FROM announcements").fetchone()[0]

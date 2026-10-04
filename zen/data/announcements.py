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

from zen.data import filing_types
from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

API = ("https://www.nseindia.com/api/corporate-announcements"
       "?index=equities&from_date={frm}&to_date={to}")

COLUMNS = ["an_dt", "trade_date", "symbol", "isin", "company", "industry",
           "category", "subject", "url", "has_xbrl", "session_date"]

# Categories that plausibly move a share price on the day. The rest is stored
# but never surfaced in the brief. These are zen.data.filing_types' names, the
# ones the stored archive carries since jobs/rederive_announcements.py. Until
# 2026-10-04 this set used the old regex vocabulary ("volume_query",
# "litigation"), which no longer occurs in the data, so exchange queries and
# regulatory filings silently never reached the brief.
MATERIAL = {"exchange_query", "results", "guidance", "expansion", "contraction",
            "orders", "mna", "capital", "ratings", "regulatory", "licenses"}

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
    -- The first session this symbol ACTUALLY traded on or after trade_date.
    -- trade_date comes from the clock alone and rolls an after-hours filing to
    -- the next calendar weekday, but a weekday need not be a trading day and a
    -- trading day for the exchange need not be one for the stock. Joining
    -- prices on trade_date silently drops 70,506 filings, skewed towards those
    -- filed before long weekends. Null where the symbol never traded again.
    session_date DATE,
    PRIMARY KEY (an_dt, symbol, subject)
);
"""


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
    # `minute >= 30`, not `> 30`. The strict form left the minute from
    # 15:30:00 to 15:30:59 attributed to the session that had just ended --
    # 1,285 filings in this archive, every one of them a one-day look-ahead,
    # and invisible because the boundary is only wrong for sixty seconds a day.
    # A filing stamped exactly at the close could not have traded on it.
    d = an_dt.date()
    if an_dt.hour >= 16 or (an_dt.hour == 15 and an_dt.minute >= 30):
        d += timedelta(days=1)
    while d.weekday() >= 5:            # roll Saturday/Sunday to Monday
        d += timedelta(days=1)
    return d


def fetch(start: date, end: date, session=None, strict: bool = False) -> pd.DataFrame:
    """Announcements published between start and end, inclusive.

    Fetched a day at a time: the endpoint truncates long ranges without
    saying so, and a silently short result here would look like "no news".
    With strict=True a failed day raises instead of being skipped, so a
    scheduled job can tell "nothing filed" from "the request failed".
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
            if strict:
                raise
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
            stored = (f"{subject}: {body}" if body else subject)[:500]
            parsed.append({
                "an_dt": an_dt,
                "trade_date": _trade_date(an_dt),
                "symbol": symbol,
                "isin": (row.get("sm_isin") or "").strip() or None,
                "company": (row.get("sm_name") or "").strip() or None,
                "industry": (row.get("smIndustry") or "").strip() or None,
                # NSE's own label decides the bucket, exactly as for the stored
                # archive (jobs/rederive_announcements.py). The regex this used
                # to run over free text put penalties in "orders".
                "category": filing_types.categorise(stored),
                "subject": stored,
                "url": (row.get("attchmntFile") or "").strip() or None,
                "has_xbrl": bool(row.get("hasXbrl")),
                # Filled by upsert() from the prices table, once the session
                # the symbol next trades on exists.
                "session_date": None,
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


KEY = ["an_dt", "symbol", "subject"]


def upsert(con, df: pd.DataFrame) -> int:
    """Insert new filings, then give every recent filing its session.

    Columns are named, not positional. The table gained session_date on
    2026-09-23 and this function kept inserting `SELECT *` from a ten-column
    frame into an eleven-column table: every daily refresh from 10 September
    failed with a Binder Error that the brief caught and logged, so the archive
    silently stopped at 9 September.
    """
    if df.empty:
        return 0
    ensure_schema(con)
    df = df.reindex(columns=COLUMNS)
    cols = ", ".join(COLUMNS)
    con.register("incoming_ann", df)
    before = con.execute("SELECT count(*) FROM announcements").fetchone()[0]
    con.execute(f"INSERT OR IGNORE INTO announcements ({cols}) "
                f"SELECT {cols} FROM incoming_ann")
    after = con.execute("SELECT count(*) FROM announcements").fetchone()[0]
    con.unregister("incoming_ann")
    fill_session_dates(con)
    return after - before


def fill_session_dates(con) -> int:
    """Set session_date where the symbol has since traded.

    The same rule as jobs/rederive_announcements.py: the first EQ or BE session
    of that symbol on or after trade_date. A filing made today has no such
    session until tomorrow's prices arrive, so it stays null until a later run.
    Every null is re-checked, not just recent ones: a stock suspended for months
    gets its session when it resumes. The permanent nulls are symbols that never
    traded again, a few thousand rows, so the join stays cheap.
    """
    ensure_schema(con)
    has_prices = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'prices'"
    ).fetchone()[0]
    if not has_prices:
        return 0
    before = con.execute(
        "SELECT count(*) FROM announcements WHERE session_date IS NULL").fetchone()[0]
    con.execute("""
        UPDATE announcements AS a SET session_date = s.session
        FROM (
            SELECT a2.an_dt, a2.symbol, a2.subject, min(p.date) AS session
            FROM announcements a2
            JOIN prices p
              ON p.symbol = a2.symbol AND p.series IN ('EQ', 'BE')
             AND p.isin_code LIKE 'INE%' AND p.date >= a2.trade_date
            WHERE a2.session_date IS NULL
            GROUP BY 1, 2, 3
        ) AS s
        WHERE a.an_dt = s.an_dt AND a.symbol = s.symbol AND a.subject = s.subject
    """)
    after = con.execute(
        "SELECT count(*) FROM announcements WHERE session_date IS NULL").fetchone()[0]
    return before - after


def for_session(con, session: date, symbols: list[str] | None = None,
                material_only: bool = True) -> pd.DataFrame:
    """Filings that could have moved a given session, optionally for a subset.

    Filtering is on session_date: the first session the symbol actually traded
    at or after the filing. trade_date rolls after-hours filings forward by
    the clock alone and can land on a holiday, or on a day the stock did not
    trade, where no move exists to explain.
    """
    ensure_schema(con)
    sql = ["SELECT * FROM announcements WHERE session_date = ?"]
    params: list = [session]

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


def _typed(df: pd.DataFrame) -> pd.DataFrame:
    """The column types the committed parquet uses (microsecond timestamps)."""
    out = df.reindex(columns=COLUMNS).copy()
    for c in ("an_dt", "trade_date", "session_date"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[us]")
    out["has_xbrl"] = out["has_xbrl"].fillna(False).astype(bool)
    return out


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR, con=None) -> list[Path]:
    """One parquet per calendar month of trade_date, mirroring how prices are stored.

    Rows are merged into the month's file and never removed. A row already in
    the file wins over a refetched copy of it, so a stored session_date is not
    replaced by a fresh row's null. With `con`, the database is used for one
    thing only: the session_date of rows already in the file or in `df`. It
    never adds rows. An earlier version copied every database row of the month
    into the file, and since some old rows sit in the file of the month before
    their trade_date (it was recomputed in place after the 15:30 fix), that
    duplicated a key across two files and broke rebuild_from_parquet. For the
    same reason a fetched row already held by the previous month's file is not
    written again.
    """
    if df.empty:
        return []
    written = []
    months = pd.to_datetime(df["trade_date"]).map(lambda d: f"{d:%Y-%m}")
    for month, chunk in df.groupby(months):
        p = out_dir / month[:4] / f"{month}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        # 70 rows from before the 15:30 boundary fix sit in the file of the month
        # BEFORE their trade_date (their trade_date was recomputed in place).
        # A refetch of one of them must not add a second copy here.
        prev = (pd.Period(month, "M") - 1).strftime("%Y-%m")
        prev_p = out_dir / prev[:4] / f"{prev}.parquet"
        if prev_p.exists():
            held = _typed(pd.read_parquet(prev_p, columns=KEY + ["trade_date"]).assign(
                **{c: None for c in COLUMNS if c not in KEY + ["trade_date"]}))[KEY]
            chunk = _typed(chunk).merge(held, on=KEY, how="left", indicator=True)
            chunk = chunk[chunk["_merge"] == "left_only"].drop(columns="_merge")
        parts = [chunk] + ([pd.read_parquet(p)] if p.exists() else [])
        merged = pd.concat([_typed(x) for x in parts if len(x)], ignore_index=True)
        merged = merged.drop_duplicates(subset=KEY, keep="last")
        if con is not None:
            db = _typed(con.execute(
                f"SELECT {', '.join(COLUMNS)} FROM announcements "
                "WHERE strftime(trade_date, '%Y-%m') = ?", [month]).df())
            db = db[KEY + ["session_date"]].rename(columns={"session_date": "_db"})
            merged = merged.merge(db, on=KEY, how="left")
            merged["session_date"] = merged["_db"].combine_first(merged["session_date"])
            merged = merged.drop(columns="_db")
        merged = merged.reindex(columns=COLUMNS).sort_values(["an_dt", "symbol"])
        merged.to_parquet(p, index=False, compression="zstd")
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
